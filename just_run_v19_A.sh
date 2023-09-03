#!/bin/bash

source venv/bin/activate
export HIP_VISIBLE_DEVICES=0
export HSA_OVERRIDE_GFX_VERSION=11.0.0

python finetune.py \
    --base_model "meta-llama/Llama-2-7b-hf" \
    --data_path "PeanutJar/PeanutButter-Train" \
    --eval_path "PeanutJar/PeanutButter-Eval" \
    --use_second_set True \
    --prompt_template_name "alpaca_short" \
    --optim "paged_adamw_8bit" \
    --learning_rate 0.0003 \
    --num_train_epochs 5 \
    --train_4bit \
    --cutoff_len 4096 \
    --lora_r 64 \
    --lora_alpha 64 \
    --gradient_accumulation_steps 24 \
    --per_device_train_batch_size 1 \
    --use_gradient_checkpointing True \
    --group_by_length True \
    --save_total_limit 20 \
    --save_and_eval_steps 10 \
    --val_set_size 1 \
    --logging_steps 1 \
    --output_dir "./00_output/LLaMa-2-PeanutButter_v19_A-7B-QLoRA"
