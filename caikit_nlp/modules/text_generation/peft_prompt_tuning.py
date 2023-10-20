# Copyright The Caikit Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""This module contains prompt tuning through PEFT"""
# Standard
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple, Union
import gc
import json
import os
import tempfile

# Third Party
from datasets import Dataset
from datasets import IterableDataset as TransformersIterableDataset
from peft import (
    MultitaskPromptTuningConfig,
    PeftConfig,
    PeftModel,
    PeftType,
    PromptTuningConfig,
    TaskType,
    get_peft_model,
)
from transformers import (
    AutoModelForCausalLM,
    DataCollatorForLanguageModeling,
    default_data_collator,
)
from transformers.models.auto.tokenization_auto import AutoTokenizer
import numpy as np
import torch

# First Party
from caikit import get_config
from caikit.core.data_model import DataStream
from caikit.core.exceptions import error_handler
from caikit.core.modules import ModuleBase, ModuleConfig, ModuleSaver, module
from caikit.interfaces.nlp.data_model import (
    ClassificationTrainRecord,
    GeneratedTextResult,
    GeneratedTextStreamResult,
)
from caikit.interfaces.nlp.tasks import TextGenerationTask
import alog

# Local
from ...data_model import (
    ExponentialDecayLengthPenalty,
    GenerationTrainRecord,
    PromptOutputModelType,
    TuningConfig,
)
from ...resources.pretrained_model import HFAutoCausalLM, HFAutoSeq2SeqLM
from ...toolkit.data_type_utils import get_torch_dtype, str_to_torch_dtype
from ...toolkit.task_specific_utils import convert_to_generation_record
from ...toolkit.text_generation.model_run_utils import (
    GENERATE_FUNCTION_ARGS,
    generate_text_func,
    generate_text_func_stream,
)
from ...toolkit.text_generation.training_utils import (
    ALLOWED_TRAINING_ARGS,
    collect_trainer_arguments,
    infer_max_steps,
    launch_training,
    preprocess_function,
)
from ...toolkit.verbalizer_utils import render_verbalizer
from ...toolkit.torch_run import get_torch_elastic_launch_config
from .peft_config import TuningType, get_peft_config, resolve_base_model

log = alog.use_channel("PEFT_PROMPT")
error = error_handler.get(log)

TRAINING_LOSS_LOG_FILENAME = "training_logs.jsonl"

# TODO: try to refactor this into a smaller module
# pylint: disable=too-many-lines,too-many-instance-attributes
@module(
    id="6655831b-960a-4dc5-8df4-867026e2cd41",
    name="Peft generation",
    version="0.1.0",
    task=TextGenerationTask,
)
class PeftPromptTuning(ModuleBase):

    _DETECT_DEVICE = "__DETECT__"
    _ENCODER_KEY = PromptOutputModelType.ENCODER
    _DECODER_KEY = PromptOutputModelType.DECODER
    _ADAPTER_NAME = "default"

    tuning_type_to_huggingface = {
        TuningType.PROMPT_TUNING: PeftType.PROMPT_TUNING,
        TuningType.MULTITASK_PROMPT_TUNING: PeftType.MULTITASK_PROMPT_TUNING,
        # TuningType.MULTITASK_PREFIX_TUNING: PeftType.MULTITASK_PREFIX_TUNING,
        # TuningType.P_TUNING: PeftType.P_TUNING,
        # TuningType.PREFIX_TUNING: PeftType.PREFIX_TUNING,
        # TuningType.LORA: PeftType.LORA,
    }

    RANDOM_SEED = 73
    supported_resources = [HFAutoCausalLM, HFAutoSeq2SeqLM]

    ################################ Constructor / Destructor #####################################

    def __init__(
        self,
        tokenizer: AutoTokenizer,
        model: PeftModel,
        base_model_config: Dict[str, Any],
        base_model_name: str,
        verbalizer: str,
        task_type: str,
        tuning_type: TuningType,
        output_model_types: List[PromptOutputModelType],
        training_metadata: Union[Dict[str, Any], None] = None,
    ):
        super().__init__()
        # Put the PEFT model into evaluation mode for all future calls
        model.eval()
        self._collate_fn = self._get_collate_fn(tokenizer, task_type)
        self.model = model
        self.tokenizer = tokenizer
        self.base_model_name = base_model_name
        self._base_model_config = base_model_config
        self.eos_token_id = self.tokenizer.encode(self.tokenizer.eos_token)[-1]
        self.verbalizer = verbalizer
        self.task_type = task_type
        self.tuning_type = tuning_type
        self.output_model_types = output_model_types
        self.training_metadata = (
            training_metadata if training_metadata is not None else {}
        )

    # pylint: disable=duplicate-code
    def __del__(self):
        del self.model
        del self.tokenizer
        gc.collect()
        try:
            torch.cuda.empty_cache()
        except AttributeError:
            pass

    ################################## API functions #############################################

    @TextGenerationTask.taskmethod()
    def run(
        self,
        text: str,
        max_new_tokens: Optional[int] = 20,
        min_new_tokens: Optional[int] = 0,
        truncate_input_tokens: Optional[int] = 0,
        decoding_method: Optional[str] = "GREEDY",
        top_k: Optional[int] = 0,
        top_p: Optional[float] = 1.0,
        typical_p: Optional[float] = 1.0,
        temperature: Optional[float] = 1.0,
        seed: Optional[np.uint64] = None,
        repetition_penalty: Optional[float] = 1.0,
        max_time: Optional[float] = None,
        exponential_decay_length_penalty: Optional[
            Union[Tuple[int, float], ExponentialDecayLengthPenalty]
        ] = None,
        stop_sequences: Optional[List[str]] = None,
    ) -> GeneratedTextResult:
        """
            Run the full text generation model.
            Args:
                {}
            Returns:
                GeneratedTextResult
                    Generated text result produced by PEFT / Transformers.
        """.format(
            GENERATE_FUNCTION_ARGS
        )

        verbalized_text = render_verbalizer(self.verbalizer, {"input": text})

        return generate_text_func(
            self.model,
            self.tokenizer,
            self.PRODUCER_ID,
            self.tokenizer.eos_token,
            verbalized_text,
            max_new_tokens=max_new_tokens,
            min_new_tokens=min_new_tokens,
            truncate_input_tokens=truncate_input_tokens,
            decoding_method=decoding_method,
            top_k=top_k,
            top_p=top_p,
            typical_p=typical_p,
            temperature=temperature,
            seed=seed,
            repetition_penalty=repetition_penalty,
            max_time=max_time,
            exponential_decay_length_penalty=exponential_decay_length_penalty,
            stop_sequences=stop_sequences,
        )

    # NOTE: We need to disable wip decorator here otherwise we get issues in
    # proto generation for streaming. We are keeping it commented out for now,
    # to essentially document that this streaming function is WIP.
    # @wip_decorator.work_in_progress(
    #     category=wip_decorator.WipCategory.WIP, action=wip_decorator.Action.WARNING
    # )
    @TextGenerationTask.taskmethod(output_streaming=True)
    def run_stream_out(
        self,
        text: str,
        max_new_tokens=20,
        min_new_tokens=0,
        truncate_input_tokens: Optional[int] = 0,
        decoding_method: Optional[str] = "GREEDY",
        top_k: Optional[int] = 0,
        top_p: Optional[float] = 0.0,
        typical_p: Optional[float] = 0.0,
        temperature: Optional[float] = 1.0,
        seed: Optional[np.uint64] = None,
        repetition_penalty: Optional[float] = 0.0,
        max_time: Optional[float] = None,
        exponential_decay_length_penalty: Optional[
            Union[Tuple[int, float], ExponentialDecayLengthPenalty]
        ] = None,
        stop_sequences: Optional[List[str]] = None,
    ) -> Iterable[GeneratedTextStreamResult]:
        """Run the text generation model with output streaming

        NOTE: This implementation is marked as WIP since the API for
        HuggingFace streamer classes at time of implementation is still
        under development and may change.
        Ref. https://huggingface.co/docs/transformers/v4.30.0/generation_strategies#streaming

        Args:
            {}

        Returns:
            Iterable[GeneratedTextStreamResult]
        """.format(
            GENERATE_FUNCTION_ARGS
        )

        # Apply the verbalizer to our text string
        verbalized_text = render_verbalizer(self.verbalizer, {"input": text})

        return generate_text_func_stream(
            self.model,
            self.tokenizer,
            self.PRODUCER_ID,
            self.tokenizer.eos_token,
            verbalized_text,
            max_new_tokens=max_new_tokens,
            min_new_tokens=min_new_tokens,
            truncate_input_tokens=truncate_input_tokens,
            decoding_method=decoding_method,
            top_k=top_k,
            top_p=top_p,
            typical_p=typical_p,
            temperature=temperature,
            seed=seed,
            repetition_penalty=repetition_penalty,
            max_time=max_time,
            exponential_decay_length_penalty=exponential_decay_length_penalty,
            stop_sequences=stop_sequences,
        )

    @classmethod
    def train(
        cls,
        base_model: str,  # TODO: Union[str, PretrainedModelBase]
        train_stream: Union[
            DataStream[GenerationTrainRecord],
            DataStream[ClassificationTrainRecord],
        ],
        tuning_config: TuningConfig,
        val_stream: Optional[
            Union[
                DataStream[GenerationTrainRecord],
                DataStream[ClassificationTrainRecord],
            ]
        ] = None,  # TODO: Optional[DataStream[GenerationTrainRecord]]
        device: Optional[str] = _DETECT_DEVICE,  # TODO: Union[int, str]
        tuning_type: Optional[str] = "PROMPT_TUNING",  # TODO: Union[str, TuningType]
        num_epochs: Optional[int] = 20,
        learning_rate: Optional[float] = 0.3,
        verbalizer: Optional[str] = "{{input}}",
        batch_size: Optional[int] = 8,
        max_source_length: Optional[int] = 256,
        max_target_length: Optional[int] = 128,
        accumulate_steps: Optional[int] = 32,
        torch_dtype: Optional[str] = None,  # TODO: Optional[Union[torch.dtype, str]]
        silence_progress_bars: Optional[bool] = True,
        random_seed: int = RANDOM_SEED,
        **kwargs,
    ) -> "PeftPromptTuning":
        """Run prompt tuning (vanilla or MPT) through PEFT on a CausalLM or Seq2seq model
        to refine a text generation model.

        Args:
            base_model:  Union[str, caikit_nlp.resources.pretrained_model.base.PretrainedModelBase]
                Base resource model used for underlying generation.
            train_stream: DataStream[GenerationTrainRecord] or DataStream[ClassificationTrainRecord]
                Data to be used for training the prompt vectors of the generation model.
            tuning_config: TuningConfig
                Additional model tuning configurations to be considered for prompt vector
                initialization and training behavior.
            val_stream: Optional[DataStream[GenerationTrainRecord]
                           or DataStream[ClassificationTrainRecord]]
                Data to be used for validation throughout the train process or None.
            device: str
                Device to be used for training the model. Default: cls._DETECT_DEVICE, which
                will fall back to "cuda" if available, else None.
            tuning_type: str
                Type of Peft Tuning config which we would like to build.
            num_epochs: int
                Number of epochs to tune the prompt vectors. Default: 20.
            learning_rate: float
                Learning rate to be used while tuning prompt vectors. Default: 1e-3.
            verbalizer: str
                Verbalizer template to be used for formatting data at train and inference time.
                This template may use brackets to indicate where fields from the data model
                TrainGenerationRecord must be rendered. Default: "{{input}}", i.e., the raw text.
            batch_size: int
                Batch sized to be used for training / evaluation data. Default: 8.
            max_source_length: int
                Max length of input sequences being considered. Default: 256.
            max_target_length: int
                Max length of target sequences being predicted. Default: 128.
            accumulate_steps: int
                Number of steps to use for gradient accumulation. Default: 1.
            torch_dtype: str
                TODO: Optional[Union[torch.dtype, str]]
                Data type to use for training/inference of the underlying text generation model.
                If no value is provided, we pull from torch_dtype in config. If an in memory
                resource is provided which does not match the specified data type, the model
                underpinning the resource will be converted in place to the correct torch dtype.
            silence_progress_bars: bool
                Silences TQDM progress bars at train time. Default: True.
        Returns:
            PeftPromptTuning
                Instance of this class with tuned prompt vectors.
        """

        torch_dtype = get_torch_dtype(torch_dtype)

        # NOTE: We are not support "metrics" at the moment

        # Coerce the passed model into a resource; if we have one, this is a noop
        # TODO: When splitting up this mono-module, use the configured resource
        #   type of the concrete class to bootstrap
        base_model = resolve_base_model(base_model, cls, torch_dtype)
        # Enable gradient checkpointing on base model
        # PeftModel checks if the base_model has gradient checkpointing
        # enabled and then configures the tensors it creates with appropriate
        # setting. If we do not enable this, then we will get `tensor 0` requires
        # grad error, where `tensor 0` is created by peft
        base_model.model.gradient_checkpointing_enable()

        base_model_name = base_model._model_name
        # Get config of the base model
        base_model_config = base_model.get_config()

        task_type, output_model_types, peft_config, tuning_type = get_peft_config(
            tuning_type,
            tuning_config,
            base_model,
            cls,
            torch_dtype,
            verbalizer,
        )

        train_stream = train_stream.map(convert_to_generation_record)
        if val_stream:
            val_stream = val_stream.map(convert_to_generation_record)

        log.debug("Peft config [%s]", peft_config)
        # FIXME: Should only do following line for causal LM (and bloomz?) - check that is the case
        if isinstance(base_model, HFAutoCausalLM):
            base_model.model.config.d_model = 1024

        peft_model = get_peft_model(base_model.model, peft_config)

        # Convert our Peft model (not just the underlying
        # transformers model) to the right underlying type.
        device = cls._get_device(device)
        # cls.convert_peft_model_to_type(device, peft_model, torch_dtype)

        ## Generate data loader from stream
        training_dataset: Union[
            Dataset, TransformersIterableDataset
        ] = preprocess_function(
            base_model=base_model,
            train_stream=train_stream,
            tokenizer=base_model.tokenizer,
            max_source_length=max_source_length,
            max_target_length=max_target_length,
            shuffle=True,
            use_iterable_dataset=False,
            random_seed=cls.RANDOM_SEED,
            task_ids=0,
        )

        # Filter **training_arguments to only process allowed ones
        filtered_training_arguments = {
            k: v for k, v in kwargs.items() if k in ALLOWED_TRAINING_ARGS
        }

        extra_training_args = set(kwargs.keys()).difference(
            filtered_training_arguments.keys()
        )

        if extra_training_args:
            log.warning(
                "<NLP18647619W>",
                f"{extra_training_args} parameter(s) not allowed by \
                    {cls.__name__} currently and will be ignored!",
            )

        if num_epochs < 1:
            log.warning(
                "<NLP41930186W>",
                f"Number of epochs configured is {num_epochs} which is less than minimum 1. \
                    No training will be performed",
            )

            return PeftPromptTuning(
                tokenizer=base_model.tokenizer,
                model=peft_model,
                base_model_config=base_model_config,
                base_model_name=base_model_name,
                verbalizer=verbalizer,
                task_type=task_type,
                tuning_type=tuning_type,
                output_model_types=output_model_types,
                training_metadata={"loss": []},
            )

        processing_configuration = {}

        # Conditionally enable sharding if multiple GPUs available
        if torch.cuda.is_available() and torch.cuda.device_count() > 1:
            processing_configuration = {
                "fsdp": "full_shard offload auto_wrap",
                "fsdp_config": {
                    # NOTE: Every transformers model has `_no_split_modules` property that can be
                    # leveraged to identify the layers to split. This seems to be a decent
                    # "default" behavior unless we want to optimize further. We will start with
                    # this generic approach, since it allows us to handle variety
                    # of models and iterate on it, based on what we encounter.
                    "fsdp_transformer_layer_cls_to_wrap": base_model._model._no_split_modules
                },
            }

        # Open an intermediate checkpoint directory until we've bootstrapped
        # our model or we've early exited (if epochs < 1)
        with tempfile.TemporaryDirectory() as checkpoint_dir:

            training_args = collect_trainer_arguments(
                torch_dtype,
                checkpoint_dir,
                batch_size,
                num_epochs,
                random_seed,
                learning_rate,
                accumulate_steps,
                max_steps=infer_max_steps(num_epochs, batch_size, training_dataset),
                silence_progress_bars=silence_progress_bars,
                # NOTE: following can override above arguments in order
                **filtered_training_arguments,
                **processing_configuration,
            )

            base_model_trainer = base_model.get_trainer(
                train_dataset=training_dataset, model=peft_model, **training_args
            )

            if torch.cuda.is_available():
                # NOTE: torch distributed can hang if run on CPUs,
                # to avoid that, specially for unit tests, we are only
                # running below when GPUs are available
                launch_config = get_torch_elastic_launch_config(
                    get_config().master_addr,
                    get_config().master_port,
                )
                training_loss_history = torch.distributed.launcher.api.elastic_launch(
                    launch_config, launch_training
                )(
                    peft_model,
                    training_dataset,
                    training_args,
                    checkpoint_dir,
                    trainer=base_model_trainer,
                    tokenizer=base_model.tokenizer
                )
                # NOTE: We are currently only storing the loss information from
                # rank 0, i.e main process. training_loss_history is dictionary containing
                # rank of the process as key
                training_loss_history = training_loss_history[0]
            else:
                # Do not do FSDP things
                training_loss_history = launch_training(
                    peft_model,
                    training_dataset,
                    training_args,
                    checkpoint_dir,
                    trainer=base_model_trainer,
                    tokenizer=base_model.tokenizer,
                )

        # Remove _name_or_path field as a model can be
        # saved in different location but still same
        del base_model_config["_name_or_path"]
        error.value_check(
            "<NLP07232147E>",
            "_name_or_path" not in base_model_config,
            "_name_or_path needs to be removed from config!",
        )

        # Wrap up the trained model in a class instance
        return cls(
            tokenizer=base_model.tokenizer,
            model=peft_model,
            base_model_config=base_model_config,
            base_model_name=base_model_name,
            verbalizer=verbalizer,
            task_type=task_type,
            tuning_type=tuning_type,
            output_model_types=output_model_types,
            training_metadata={"loss": training_loss_history},
            # TODO: Export other training params to model as well
        )

    def save(self, model_path: str, save_base_model: bool = False):
        """Save prompt vector and optionally base model in target path

        Args:
            model_path: str
                Path to store model artifact(s)
            save_base_model: bool
                Save base model along with the prompts in the model_path provided.
                Default: False
        """
        module_saver = ModuleSaver(
            self,
            model_path=model_path,
        )

        # NOTE: In case we want optionally allow saving of the base model with the prompts
        # we can use the `base_model.save` method as its a resource that
        # implements its own save method
        prompt_dict = self.get_exportable_prompt_vectors(
            self.model, self.tuning_type, self.output_model_types
        )
        assert prompt_dict, "Failed to export encoder and/or decoder prompts"
        with module_saver:
            config_options = {
                "base_model_config": self._base_model_config,
                "base_model_name": self.base_model_name,
                "eos_token": self.tokenizer.eos_token,
                "has_base_model": save_base_model,
                "verbalizer": self.verbalizer,
                "tuning_type": self.tuning_type.name,
                "task_type": str(self.task_type),
                # Grab the torch property for the dtype so that we can rebuild from a str.
                "trained_torch_dtype": str(self.model.dtype).rsplit(".", maxsplit=1)[
                    -1
                ],
                "output_model_types": json.dumps(
                    [output_type.name for output_type in self.output_model_types]
                ),
            }
            # NOTE: These file names correspond to expected file names in TGIS.
            key_file_pairs = [
                [PeftPromptTuning._ENCODER_KEY.name, "encoder.pt"],
                [PeftPromptTuning._DECODER_KEY.name, "decoder.pt"],
            ]
            for prompt_key, prompt_bin in key_file_pairs:
                prompt_save_path = os.path.realpath(
                    os.path.join(model_path, prompt_bin)
                )

                # Prompt vector (encoder or decoder) not found; set config to empty and continue
                if prompt_dict[prompt_key] is None:
                    config_options[prompt_key] = ""
                    continue

                config_options[prompt_key] = prompt_bin
                log.debug3("Saving prompt %s to: %s", prompt_key, prompt_save_path)
                with alog.ContextTimer(log.debug3, "Done saving prompt in: "):
                    torch.save(prompt_dict[prompt_key], prompt_save_path)
                assert os.path.isfile(
                    prompt_save_path
                ), f"Prompt was not successfully saved to {prompt_save_path}"
            if save_base_model:
                b_model_rel_path, b_model_abs_path = module_saver.add_dir(
                    self.base_model_name
                )
                self.tokenizer.save_pretrained(os.path.join(b_model_abs_path))
                self.model.save_pretrained(os.path.join(b_model_abs_path))

                config_options["full_model_path"] = b_model_rel_path
                config_options["tokenizer_path"] = b_model_rel_path

            training_loss_filename = TRAINING_LOSS_LOG_FILENAME

            config_options.update({"training_logs": training_loss_filename})
            # We are currently only saving logs containing loss in jsonl format
            if "loss" in self.training_metadata:
                loss_log_lines = self.training_metadata.get("loss")
                error.type_check("<NLP60269855E>", list, loss_log_lines=loss_log_lines)
                with open(
                    os.path.join(model_path, training_loss_filename),
                    "w",
                    encoding="utf-8",
                ) as f:
                    for loss_log in loss_log_lines:
                        loss_log = {"name": "loss", "data": loss_log}
                        json.dump(loss_log, f)
                        f.write("\n")

            module_saver.update_config(config_options)

    @classmethod
    def load(
        cls,
        model_path: str,
        torch_dtype: str = None,
        device: str = _DETECT_DEVICE,  # TODO: Union[int, str]
    ) -> "PeftPromptTuning":
        """Load a PEFT prompt tuning model. This method will currently fail if the original
        model was not saved with the arg value save_base_model=True.

        Args:
            model_path: str
                Path to the model to be loaded.
            torch_dtype: str
                Torch data type to be used when loading the model.

        Returns:
            PeftPromptTuning
                Instance of this class built from the on disk model.
        """
        # TODO: Fix this to only allow prompt vector execution
        config = ModuleConfig.load(os.path.abspath(model_path))
        if torch_dtype is not None:
            torch_dtype = str_to_torch_dtype(torch_dtype)
        else:
            torch_dtype = str_to_torch_dtype(config.trained_torch_dtype)
        if config.has_base_model:
            # TODO: Implement logic for resource loading
            device = cls._get_device(device)
            model_config = os.path.join(model_path, config.full_model_path)
            peft_config = PeftConfig.from_pretrained(model_config)
            if peft_config.task_type == "CAUSAL_LM":
                # get the transformers Causal LM model
                base_model = AutoModelForCausalLM.from_pretrained(
                    peft_config.base_model_name_or_path
                )
                # get the PEFT causal LM model
                model = PeftModel.from_pretrained(base_model, model_config)
                cls.convert_peft_model_to_type(device, model, torch_dtype)
            else:
                # TODO: Handle other model types
                error(
                    "<NLP84249238E>",
                    NotImplementedError("Only export of causal LM models is supported"),
                )
            tokenizer = AutoTokenizer.from_pretrained(
                os.path.join(model_path, config.tokenizer_path)
            )
        else:
            # TODO: Can we make this to be a warning and just
            # work with prompt vectors if base model is not provided
            error("<NLP97275192E>", ValueError("base_model not provided."))

        output_model_types = [
            PromptOutputModelType(output_type)
            for output_type in json.loads(config.output_model_types)
        ]

        return cls(
            tokenizer=tokenizer,
            model=model,
            base_model_config=config.base_model_config,
            base_model_name=config.base_model_name,
            verbalizer=config.verbalizer,
            task_type=config.task_type,
            tuning_type=TuningType(config.tuning_type),
            output_model_types=output_model_types,
        )

    ################################## Public Functions ###########################################

    @classmethod
    def get_exportable_prompt_vectors(
        cls,
        model: PeftModel,
        tuning_type: TuningType,
        output_model_types: List[PromptOutputModelType],
    ) -> Dict[str, torch.Tensor]:
        """Grab the prompt vectors off of the model and return a tuple of encoder / decoder
        export vectors.

        Args:
            model: PeftModel
                Model whose prompt vector(s) we want to export.
            tuning_type: TuningType
                Tuning type used to build this model.
            output_model_types: List[PromptOutputModelType]
                Output model types prompt type (eg, encoder, decoder)
        Returns:
            Dict[str, torch.Tensor]
                Dictionary mapping file names to torch tensors to be exported. If a value is
                not applicable, it will have a defined key in the produced dictionary & map to
                None.
        """
        prompt_dict = {
            PeftPromptTuning._ENCODER_KEY.name: None,
            PeftPromptTuning._DECODER_KEY.name: None,
        }
        num_transformer_submodules = model.peft_config[
            cls._ADAPTER_NAME
        ].num_transformer_submodules
        num_virtual_tokens = model.peft_config[cls._ADAPTER_NAME].num_virtual_tokens
        # Our model should only have one or two transformer modules; PEFT config lets you
        # arbitrarily configure these, but the slicing assumptions for the prompt tuning
        # seem to assume this...
        error.value_check(
            "<NLP83837722E>",
            1 <= num_transformer_submodules <= 2,
            f"Only 1 or 2 transformer submodules allowed. {num_transformer_submodules} detected.",
        )
        # Get the prompt vectors.
        if tuning_type == TuningType.PROMPT_TUNING:  # Should also be done for prefix
            # NOTE; If this is done for MPT, we get the SHARED prompt vector.
            # be careful with this, because it's the same shape as the task
            # specific tuned thing we want, and will give you garbage if you
            # leverage it directly in TGIS.
            log.info("Extracting prompt vector for prompt tuning")
            prompt_vector = model.get_prompt_embedding_to_save(
                adapter_name=cls._ADAPTER_NAME
            )
        elif tuning_type == TuningType.MULTITASK_PROMPT_TUNING:
            # For MPT / Multiprefix, run the prompt encoder, with task IDs None;
            # This assumes a single target task and produces the Hadamard product
            # of the shared prompt vector and the task learned component for Task ID 0,
            # I.e., the only task.
            prompt_tokens = (
                model.prompt_tokens[cls._ADAPTER_NAME]
                .unsqueeze(0)
                .expand(1, -1)
                .to(model.device)
            )

            log.info("Calculating single target task prompt vector")
            # Since this is running essentially an dummy forward, pass in
            # task ids as zero Tensor to forward function
            task_ids = torch.zeros(prompt_tokens.shape[0], dtype=torch.long).to(
                model.device
            )
            prompt_vector = torch.squeeze(
                model.prompt_encoder[cls._ADAPTER_NAME].forward(
                    prompt_tokens, task_ids=task_ids
                ),
                dim=0,
            )
        # Ensure that our prompt vector is on the same device as our model
        prompt_vector = prompt_vector.to(model.device)
        # Each transformer submodule should have num_virtual_tokens rows
        error.value_check(
            "<NLP83444722E>",
            prompt_vector.shape[0] == num_transformer_submodules * num_virtual_tokens,
            f"Row mismatch: Expected num_transformer_submodules * num_virtual_tokens "
            f"({num_transformer_submodules * num_virtual_tokens}) "
            f"but got {prompt_vector.shape[0]}",
        )

        # Otherwise it depends on the number of transformer modules. See seq2seq forward()

        # For Causal-LM we will essentially consider entire matrix, which has num_virtual_tokens
        # rows as the output and for Seq2Seq we will consider the 1st half of the matrix, which
        # currently has duplicate values in the second half.

        for output_type in output_model_types:
            prompt_dict[output_type.name] = prompt_vector[:num_virtual_tokens]

        return prompt_dict

    @classmethod
    def create_hf_tuning_config(
        cls,
        base_model,
        tuning_type: TuningType,
        task_type: str,
        tokenizer_name_or_path: str,
        tuning_config: TuningConfig,
        output_model_types: List[PromptOutputModelType],
    ) -> PromptTuningConfig:
        """Creates Huggingface PromptTuningConfig from Caikit tuning configuration.

        Args:
            base_model: PretrainedModelBase
                Base model resource used for prompt tuning
            tuning_type: TuningType
                Type of Peft Tuning config which we would like to build.
            task_type: str
                String identifier for peft.TaskType enum, e.g., SEQ_2_SEQ_LM, CAUSAL_LM.
            tokenizer_name_or_path: str
                Name or path to the tokenizer to be leveraged.
            tuning_config: TuningConfig
                Additional model tuning configurations to be considered for prompt vector
                initialization and training behavior.
            output_model_types: List[PromptOutputModelType]
                List of output model types supported

        Returns:
            peft.PromptTuningConfig
                Peft config to be used for initializing single/multi prompt tuning.
        """

        # NOTE: Should num_virtual_tokens be part of direct `train` function param instead
        # of tuning_config?
        # NOTE: We are currently not supporting random initialization, i.e prompt_tuning_init.Random
        error.type_check("<NLP61851758E>", str, task_type=task_type)
        error.type_check("<NLP37352293E>", TuningConfig, tuning_config=tuning_config)

        error.value_check(
            "<NLP11369136E>",
            tuning_config.num_virtual_tokens
            and isinstance(tuning_config.num_virtual_tokens, int),
            "num_virtual_tokens not provided in tuning_config",
        )

        config_kwargs = tuning_config.to_dict()
        # NOTE: We are doing the mapping of state_dict_path to init_source_model
        # because we have renamed the name of that parameter in our API
        config_kwargs[
            "prompt_tuning_init_state_dict_path"
        ] = tuning_config.prompt_tuning_init_source_model

        task_type_hf = TaskType(task_type)

        config_kwargs["tokenizer_name_or_path"] = tokenizer_name_or_path
        config_kwargs[
            "num_transformer_submodules"
        ] = base_model.get_num_transformers_submodules(output_model_types)

        if tuning_config.prompt_tuning_init_method:
            config_kwargs[
                "prompt_tuning_init"
            ] = tuning_config.prompt_tuning_init_method

        if tuning_config.prompt_tuning_init_text:
            config_kwargs[
                "prompt_tuning_init_text"
            ] = tuning_config.prompt_tuning_init_text

        if tuning_type == TuningType.PROMPT_TUNING:
            tuning_config_type = PromptTuningConfig
        # elif tuning_type == TuningType.PREFIX_TUNING:
        #     tuning_config_type = PrefixTuningConfig
        elif tuning_type == TuningType.MULTITASK_PROMPT_TUNING:
            tuning_config_type = MultitaskPromptTuningConfig

        config_params = cls._filter_params_for_prompt_config(
            tuning_config_type, config_kwargs
        )
        log.info("<NLP41038481I>", f"Parameters used: {config_params}")
        return tuning_config_type(task_type=task_type_hf, **config_params)

    ################################## Private Functions ###########################################

    @classmethod
    def _get_device(cls, device: Optional[Union[str, int]]) -> Union[str, int, None]:
        """Get the device which we expect to run our models on. Defaults to GPU
        if one is available, otherwise falls back to None (cpu).

        Args:
            device: Optional[Union[str, int]]
                Device to be leveraged; if set to cls._DETECT_DEVICE, infers the device,
                otherwise we simply echo the value, which generally indicates a user override.

        Returns:
            Union[str, int, None]
                Device string that we should move our models / tensors .to() at training
                and inference time.
        """
        if device == cls._DETECT_DEVICE:
            device = "cuda" if torch.cuda.is_available() else None
            log.debug("Using device: %s", device)
        return device

    # pylint: disable=unused-argument
    @staticmethod
    def _get_collate_fn(tokenizer: AutoTokenizer, task_type: str) -> Callable:
        """Simple layer of indirection in case we want to patch in additional collate functions
        easily. Currently we always fall back to the simple default in Transformers.

        args:
            tokenizer: AutoTokenizer
                Model tokenizer. Currently this is not used, but we pass it anyway in case
                additional collate_fns dependent on it are implemented here.
            task_type: str
                Task type to be used for data collation; used for data collator overrides.

        Returns:
            Callable
                collate_fn to be used for processing batches from our datasets.
        """
        if task_type == "CAUSAL_LM":
            return DataCollatorForLanguageModeling(
                tokenizer=tokenizer,
                return_tensors="pt",
                mlm=False,
            )
        return default_data_collator

    @classmethod
    def _filter_params_for_prompt_config(cls, prompt_config, params):
        """Utility function to filter out required parameters for prompt_config
        from `params`

        Args:
            prompt_config: PromptTuningConfig
                Tuning config type, eg:, PromptTuningConfig
            params: dict
                Dictionary containing all the input training params

        Returns:
            dict:
                Dictionary containing required params for prompt_config
        """
        # Inspect the underlying dataclass fileds; we do this because the common super class
        # used for multi/vanilla prompt/prefix tuning is a DataClass; we can't use __dict__
        # because the dataclass fields are omitted.
        allowed_keys = list(prompt_config.__dataclass_fields__.keys())
        allowed_params = dict(filter(lambda x: x[0] in allowed_keys, params.items()))
        log.info(
            "<NLP18184771I>",
            "[{}] config params not supported by provided tuning type!".format(
                params.keys() - allowed_params.keys()
            ),
        )
        return allowed_params

    @staticmethod
    def convert_peft_model_to_type(
        device: str, peft_model: PeftModel, torch_dtype=Union[str, torch.dtype]
    ) -> None:
        """Convert the underlying data type of this model to the passed dtype.

        Args:
            device: str
                Device to move our model onto.
            peft_model: PeftModel
                Model to be moved and converted to the provided PyTorch type.
            torch_dtype: Union[str, torch.dtype]
                Torch data type that we would like to coerce our model reference to.
        """
        error.type_check("<NLP83837212E>", str, allow_none=True, device=device)
        error.type_check("<NLP83837222E>", PeftModel, peft_model=peft_model)
        error.type_check("<NLP83837232E>", torch.dtype, str, torch_dtype=torch_dtype)
        # Get the actual torch type and validate, e.g., if we passed a string,
        # then move the peft model to that type on our training device.
        torch_dtype = get_torch_dtype(torch_dtype)
        # If our requested dtype is bfloat16 & we don't support it, fall back to float32
        if torch_dtype == torch.bfloat16 and (
            device == "cpu" or not torch.cuda.is_bf16_supported()
        ):
            log.warning(
                "<NLP18555772W>",
                "Requested data type torch.bfloat16 is unsupported; falling back to torch.float32",
            )
            torch_dtype = torch.float32
        peft_model.to(device, torch_dtype)
