import os
import warnings
from typing import List

import fire
import torch
import transformers
from transformers import BitsAndBytesConfig, AutoTokenizer, AutoModelForCausalLM
from datasets import load_dataset, load_from_disk

from peft import (
    LoraConfig,
    get_peft_model,
    prepare_model_for_kbit_training,
    prepare_model_for_int8_training,
    set_peft_model_state_dict,
)

from utils.prompter import Prompter

def train(
    # model/data params - required
    base_model: str = "",
    data_path: str = "PeanutJar/PeanutButter-Train",
    eval_path: str = "PeanutJar/PeanutButter-Eval",
    use_second_set: bool = False,  # if False, split eval set from original training dataset, and `eval_path` will be ignored.
    # HF Trainer params
    output_dir: str = "./lora-alpaca",
    optim: str = "paged_adamw_8bit",
    num_train_epochs: int = 3,
    learning_rate: float = 3e-4,
    per_device_train_batch_size: int = 4,
    save_and_eval_steps: int = 10,
    warmup_ratio: float = 0.06,
    save_total_limit: int = 20,
    logging_steps: int = 1,
    seed: int = 42,
    max_grad_norm: float = 1.0,
    # faster, but produces an odd training loss curve - recommended to use
    group_by_length: bool = True,
    # use global batch size OR gradient accumulation steps, not both
    # one must NOT be 0
    gradient_accumulation_steps: int = 24,
    # alpaca-lora training hyper/params
    is_finetune: bool = False,
    fsdp_params: str = '',
    global_batch_size: int = 0,
    cutoff_len: int = 2048,
    val_set_size: int = 2000,  # set value to 1 if `use_second_set` is True
    train_fp16: bool = False,
    train_bf16: bool = False,
    train_4bit: bool = True,
    use_gradient_checkpointing: bool = True,
    use_flash_attn: bool = False,
    use_xformers: bool = False,
    use_rope: bool = False,
    # lora-specific hyperparams
    is_lora: bool = True,
    lora_r: int = 64,
    lora_alpha: int = 64,
    lora_dropout: float = 0.05,
    lora_target_modules: List[str] = [
        "gate_proj",
        "down_proj",
        "up_proj",
        "q_proj",
        "k_proj",
        "v_proj",
        "o_proj"
    ],
    # llm hyperparams
    train_on_inputs: bool = True,  # if False, masks out inputs in loss
    add_eos_token: bool = True,
    # wandb params
    wandb_project: str = "",
    wandb_run_name: str = "",
    wandb_watch: str = "",  # options: false | gradients | all
    wandb_log_model: str = "",  # options: false | true
    resume_from_checkpoint: str = None,  # either training checkpoint or final adapter
    prompt_template_name: str = "alpaca_short",  # The prompt template to use, will default to alpaca.
):
    warnings.filterwarnings('ignore', category=UserWarning, module='bitsandbytes.autograd._functions')

    # TODO: option to load config from json

    if sum([train_fp16, train_bf16, train_4bit]) >= 2:
        raise Exception("The following parameters | --train_fp16 | --train_bf16 | --train_4bit | "
                        "cannot be used at the same time.")

    if use_rope:
        from utils.monkeypatches import apply_rope_monkeypatch
        apply_rope_monkeypatch()

    if use_xformers and not use_flash_attn:
        try:
            from utils.monkeypatches import apply_xformers_monkeypatches
            apply_xformers_monkeypatches()
        except ModuleNotFoundError:
            print('Xformers module not found. Skipping')
    elif use_flash_attn and not use_xformers:
        try:
            from utils.monkeypatches import apply_flash_attention_monkeypatch
            apply_flash_attention_monkeypatch()
        except ModuleNotFoundError:
            print('flash_attn module not found. Skipping')

    prompter = Prompter(prompt_template_name)

    device_map = "auto"
    world_size = int(os.environ.get("WORLD_SIZE", 1))
    ddp = world_size != 1
    if ddp:
        device_map = {"": int(os.environ.get("LOCAL_RANK") or 0)}
        if fsdp_params != "":
            device_map = "auto"
        gradient_accumulation_steps, global_batch_size = calculate_batches(global_batch_size,
                                                                           world_size,
                                                                           gradient_accumulation_steps,
                                                                           num_devices=world_size)
    else:
        gradient_accumulation_steps, global_batch_size = calculate_batches(global_batch_size,
                                                                           per_device_train_batch_size,
                                                                           gradient_accumulation_steps)

    if int(os.environ.get("LOCAL_RANK", 0)) == 0:
        training_type = "fp16" if train_fp16 else "bf16" if train_bf16 else "4bit" if train_4bit else "8bit"
        training_method = "LoRA" if not is_finetune else "Finetune"
        print(
            f"Training model with the following params:\n"
            f"base_model: {base_model}\n"
            f"data_path: {data_path}\n"
            f"eval_path: {eval_path}\n"
            f"output_dir: {output_dir}\n"
            f"training_method: {training_method}\n"
            f"using DDP: {ddp}\n"
            f"optimizer: {optim}\n"
            f"training_type: {training_type}\n"
            f"learning_rate: {learning_rate}\n"
            f"num_train_epochs: {num_train_epochs}\n"
            f"save_and_eval_steps: {save_and_eval_steps}\n"
            f"per_device_train_batch_size: {per_device_train_batch_size}\n"
            f"gradient accumulation steps: {gradient_accumulation_steps}\n"
            f"global batch_size: {global_batch_size}\n"
            f"warmup_ratio: {warmup_ratio}\n"
            f"cutoff_len: {cutoff_len}\n"
            f"val_set_size: {val_set_size}\n"
            f"fsdp_params: {fsdp_params}\n"
        )
        if not is_finetune:
            print(
                f"lora_r: {lora_r}\n"
                f"lora_alpha: {lora_alpha}\n"
                f"lora_dropout: {lora_dropout}\n"
                f"lora_target_modules: {lora_target_modules}\n"
            )
        print(
            f"use_rope: {use_rope}\n"
            f"max_grad_norm: {max_grad_norm}\n"
            f"train_on_inputs: {train_on_inputs}\n"
            f"flash_attention_enabled: {use_flash_attn}\n"
            f"xformers_enabled: {use_xformers}\n"
            f"add_eos_token: {add_eos_token}\n"
            f"group_by_length: {group_by_length}\n"
            f"wandb_project: {wandb_project}\n"
            f"wandb_run_name: {wandb_run_name}\n"
            f"wandb_watch: {wandb_watch}\n"
            f"wandb_log_model: {wandb_log_model}\n"
            f"resume_from_checkpoint: {resume_from_checkpoint or False}\n"
            f"prompt template: {prompt_template_name}\n"
        )
    assert (
        base_model
    ), "Please specify a --base_model, e.g. --base_model='elinas/llama-7b-hf-transformers-4.29'"

    # Check if parameter passed or if set within environ
    use_wandb = len(wandb_project) > 0 or (
        "WANDB_PROJECT" in os.environ and len(os.environ["WANDB_PROJECT"]) > 0
    )
    # Only overwrite environ if wandb param passed
    if len(wandb_project) > 0:
        os.environ["WANDB_PROJECT"] = wandb_project
    if len(wandb_watch) > 0:
        os.environ["WANDB_WATCH"] = wandb_watch
    if len(wandb_log_model) > 0:
        os.environ["WANDB_LOG_MODEL"] = wandb_log_model

    # check if the user wants to train in fp16 to adjust the way the model is loaded
    # note that it will finetune in 8bit unless specified
    load_in_8bit = not (train_fp16 or train_bf16)
    torch_dtype = torch.float16 if train_fp16 else torch.bfloat16

    if not train_4bit:
        model = AutoModelForCausalLM.from_pretrained(
            base_model,
            load_in_8bit=load_in_8bit,
            torch_dtype=torch_dtype,
            device_map=device_map,
        )
    else:
        # assume that default is 8bit, fp16 is optional (above) and the last option is 4bit
        # https://huggingface.co/blog/4bit-transformers-bitsandbytes#nested-quantization
        nf4_config = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            # bnb_4bit_use_double_quant=True,
            bnb_4bit_compute_dtype=torch.bfloat16
        )
        model = AutoModelForCausalLM.from_pretrained(
            base_model,
            quantization_config=nf4_config
        )

    tokenizer = AutoTokenizer.from_pretrained(base_model)

    tokenizer.pad_token_id = (
        0  # unk. we want this to be different from the eos token
    )
    tokenizer.padding_side = "left"  # Allow batched inference

    def tokenize(prompt, add_eos_token=True):
        # there's probably a way to do this with the tokenizer settings
        # but again, gotta move fast
        result = tokenizer(
            prompt,
            truncation=True,
            max_length=cutoff_len,
            padding=False,
            return_tensors=None,
        )
        if (
            result["input_ids"][-1] != tokenizer.eos_token_id
            and len(result["input_ids"]) < cutoff_len
            and add_eos_token
        ):
            result["input_ids"].append(tokenizer.eos_token_id)
            result["attention_mask"].append(1)

        result["labels"] = result["input_ids"].copy()

        return result

    def generate_and_tokenize_prompt(data_point):
        full_prompt = prompter.generate_prompt(
            data_point["instruction"],
            data_point["input"],
            data_point["output"],
        )
        tokenized_full_prompt = tokenize(full_prompt)
        if not train_on_inputs:
            user_prompt = prompter.generate_prompt(
                data_point["instruction"], data_point["input"]
            )
            tokenized_user_prompt = tokenize(
                user_prompt, add_eos_token=add_eos_token
            )
            user_prompt_len = len(tokenized_user_prompt["input_ids"])

            if add_eos_token:
                user_prompt_len -= 1

            tokenized_full_prompt["labels"] = [
                -100
            ] * user_prompt_len + tokenized_full_prompt["labels"][
                user_prompt_len:
            ]  # could be sped up, probably
        return tokenized_full_prompt

    if use_gradient_checkpointing:
        model.gradient_checkpointing_enable()

    if not is_finetune and (not train_fp16 or not train_bf16):
        if train_4bit:
            model = prepare_model_for_kbit_training(model)
        else:
            model = prepare_model_for_int8_training(model)

    if not is_finetune:
        config = LoraConfig(
            r=lora_r,
            lora_alpha=lora_alpha,
            target_modules=lora_target_modules,
            lora_dropout=lora_dropout,
            bias="none",
            task_type="CAUSAL_LM",
        )
        model = get_peft_model(model, config)

    if not ddp and torch.cuda.device_count() > 1:
        # keeps Trainer from trying its own DataParallelism when more than 1 gpu is available
        # TODO LOOK INTO THIS VS PASSING fsdp + fsdp_config
        #  https://huggingface.co/docs/transformers/main_classes/trainer#transformers.TrainingArguments.fsdp
        if fsdp_params == "":
            model.is_parallelizable = True
            model.model_parallel = True

    if data_path.endswith(".json") or data_path.endswith(".jsonl"):
        data = load_dataset("json", data_files=data_path)
    else:
        data = load_dataset(data_path)
    
    if use_second_set is True:
        if eval_path.endswith(".json") or eval_path.endswith(".jsonl"):
            eval_data = load_dataset("json", data_files=eval_path)
        else:
            eval_data = load_dataset(eval_path)

    if resume_from_checkpoint:
        # Check the available weights and load them
        checkpoint_name = os.path.join(
            resume_from_checkpoint, "pytorch_model.bin"
        )  # Full checkpoint
        print("Resuming from full checkpoint")

        if not os.path.exists(checkpoint_name) and not is_finetune:
            checkpoint_name = os.path.join(
                resume_from_checkpoint, "adapter_model.bin"
            )  # only LoRA model - LoRA config above has to fit
            print("Actually resuming from LoRA adapter model")
            resume_from_checkpoint = (
                False  # So the trainer won't try loading its state
            )

        # The two files above have a different name depending on how they were saved, but are actually the same.
        if os.path.exists(checkpoint_name) and not is_finetune:
            print(f"Restarting from {checkpoint_name}")
            adapters_weights = torch.load(checkpoint_name)
            set_peft_model_state_dict(model, adapters_weights)
        elif not is_finetune:
            print(f"Checkpoint {checkpoint_name} not found")

    if not is_finetune:
        model.print_trainable_parameters()  # Be more transparent about the % of trainable params.

    save_to_path = f'./tokenized/{os.path.splitext(data_path)[0]}'
    if use_second_set is True:
        save_to_eval = f'./tokenized/{os.path.splitext(eval_path)[0]}'
    if val_set_size > 0:
        if not os.path.exists(save_to_path) and not os.path.exists(f"{save_to_path}_val"):
            print("Tokenizing new dataset split for train and validation")
            if use_second_set is False:
                train_val = data["train"].train_test_split(
                    test_size=val_set_size, shuffle=True, seed=42
                )
                val_data = (
                    train_val["test"].shuffle().map(generate_and_tokenize_prompt)
                )
                train_data = (
                    train_val["train"].shuffle().map(generate_and_tokenize_prompt)
                )
            elif use_second_set is True:
                train_data = data["train"].shuffle().map(generate_and_tokenize_prompt)
                val_data = eval_data["test"].shuffle().map(generate_and_tokenize_prompt)
            
            train_data.save_to_disk(save_to_path)
            val_data.save_to_disk(save_to_eval)
        else:
            print("Loading original tokenized train and val datasets")
            train_data = load_from_disk(save_to_path)
            val_data = load_from_disk(save_to_eval)
    else:
        if not os.path.exists(save_to_path):
            print("Tokenizing new dataset")
            train_data = data["train"].shuffle().map(generate_and_tokenize_prompt)
            train_data.save_to_disk(save_to_path)
        else:
            print("Loading original tokenized")
            train_data = load_from_disk(save_to_path)
        val_data = None


    # if we're finetuning, we don't need the peft model callback
    callbacks = [SavePeftModelCallback]
    if is_finetune:
        callbacks = None

    if fsdp_params == '':
        fsdp_params = False



    # https://huggingface.co/docs/transformers/main_classes/trainer#transformers.Trainer
    args = transformers.TrainingArguments(
        per_device_train_batch_size=per_device_train_batch_size,
        gradient_accumulation_steps=gradient_accumulation_steps,
        warmup_ratio=warmup_ratio,  # default 0.06 as recommended by MS LoRA
        num_train_epochs=num_train_epochs,
        learning_rate=learning_rate,
        lr_scheduler_type="constant_with_warmup",
        fp16=True if not train_bf16 else False,  # mixed precision, bf16 seems like a good option as well
        bf16=train_bf16,
        logging_steps=logging_steps,
        optim=optim,
        evaluation_strategy="steps" if val_set_size > 0 else "no",
        save_strategy="steps",
        eval_steps=save_and_eval_steps if val_set_size > 0 else None,
        save_steps=save_and_eval_steps,
        output_dir=output_dir,
        save_total_limit=save_total_limit,
        load_best_model_at_end=True if val_set_size > 0 else False,
        ddp_find_unused_parameters=False if ddp else None,
        group_by_length=group_by_length,
        # ddp_timeout=1800,
        report_to="wandb" if use_wandb else None,
        run_name=wandb_run_name if use_wandb else None,
        seed=seed,
        max_grad_norm=max_grad_norm,  # if not use_xformers else max_grad_norm if max_grad_norm != 1.0 else 0.5
        fsdp=fsdp_params
        # **vars(training_args)
    )
    # accelerate.Accelerator(mixed_precision='fp8')

    trainer = transformers.Trainer(
        model=model,
        train_dataset=train_data,
        eval_dataset=val_data,
        args=args,
        callbacks=callbacks,
        data_collator=transformers.DataCollatorForSeq2Seq(
            tokenizer, pad_to_multiple_of=8, return_tensors="pt", padding=True
        )
    )

    # silences warnings, only enable for inference
    model.config.use_cache = False

    # Read more on torch.compile here and the performance improvements:
    # It currently is not supported on Windows
    # https://pytorch.org/get-started/pytorch-2.0/#pytorch-2x-faster-more-pythonic-and-as-dynamic-as-ever
    # if torch.__version__ >= "2" and sys.platform != "win32":
    #     model = torch.compile(model)

    trainer.train(resume_from_checkpoint=resume_from_checkpoint)
    # saves the trainer state parameters
    trainer.save_state()
    # saves the model weights to be loaded from_pretrained or resuming training
    trainer.save_model()

    if not is_finetune:
        # this saves the final LoRA adapter at the end of training in the main directory specified
        model.save_pretrained(output_dir)


def calculate_batches(global_batch_size=0, per_device_train_batch_size=1, gradient_accumulation_steps=1, num_devices=1):
    """
    Calculates the gradient_accumulation_steps to use depending on if the global_batch_size is defined or return
    the gradient_accumulation_steps if it's the default of 0 meaning it was not used as a parameter

    Either gradient_accumulation_steps or global_batch_size must be defined to return the gradient_accumulation_steps
    as well as the global_batch_size
    """
    if gradient_accumulation_steps != 0:
        global_batch_size = per_device_train_batch_size * num_devices * gradient_accumulation_steps
        return gradient_accumulation_steps, global_batch_size
    elif global_batch_size != 0:
        gradient_accumulation_steps = max(global_batch_size // per_device_train_batch_size, 1)
        return gradient_accumulation_steps, global_batch_size
    else:
        raise Exception('Either --gradient_accumulation_steps or --global_batch_size must be provided.')


# borrowed from https://github.com/PygmalionAI/training-code/blob/main/training/hf_trainer.py
class SavePeftModelCallback(transformers.TrainerCallback):
    """
    At some point, PEFT stopped saving just the adapter and instead started
    storing full model weights. Extracting the adapter from the weights is
    doable, but seems to result in subpar results for some unknown reason, so
    this Trainer callback saves the adapter itself during training to avoid
    this.

    https://github.com/huggingface/peft/issues/286#issuecomment-1512611968
    https://github.com/huggingface/peft/blob/main/examples/int8_training/peft_bnb_whisper_large_v2_training.ipynb
    """

    def on_save(
        self,
        args: transformers.TrainingArguments,
        state: transformers.TrainerState,
        control: transformers.TrainerControl,
        **kwargs,
    ):
        checkpoint_folder_name = f"{transformers.trainer_utils.PREFIX_CHECKPOINT_DIR}-{state.global_step}"
        checkpoint_folder = os.path.join(args.output_dir, checkpoint_folder_name)

        peft_model_path = os.path.join(checkpoint_folder, "adapter_model")
        kwargs["model"].save_pretrained(peft_model_path)

        # pytorch_model_path = os.path.join(checkpoint_folder, "pytorch_model.bin")
        # if os.path.exists(pytorch_model_path):
        #     os.remove(pytorch_model_path)

        return control


if __name__ == "__main__":
    fire.Fire(train)
