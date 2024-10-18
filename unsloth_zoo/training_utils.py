# Unsloth Zoo
# Copyright (C) 2024-present the Unsloth AI team. All rights reserved.

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.

# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

import os
import torch
import math
from transformers import set_seed as transformers_set_seed
from transformers import get_scheduler as transformers_get_scheduler
from transformers import Trainer
from transformers.trainer import OPTIMIZER_NAME, SCHEDULER_NAME, TRAINER_STATE_NAME
from transformers.trainer_utils import seed_worker as trainer_utils_seed_worker
from transformers.trainer_utils import get_last_checkpoint, PREFIX_CHECKPOINT_DIR
from transformers.trainer_callback import TrainerState
from tqdm import tqdm as ProgressBar
from packaging.version import Version
from transformers.models.llama.modeling_llama import logger
import time

__all__ = [
    "fix_zero_training_loss",
    "unsloth_train",
]


@torch.inference_mode
def fix_zero_training_loss(model, tokenizer, train_dataset):
    """
    Sometimes the labels get masked by all -100s, causing the loss
    to be 0. We check for this!
    """
    if len(train_dataset) == 0: return

    row = train_dataset[0]
    if type(row) is dict and "labels" in row:

        # Check the first 100 rows
        seen_bad  = 0
        seen_good = 0
        for i, row in enumerate(train_dataset):
            try:    check_tokens = list(set(row["labels"]))
            except: continue
            if len(check_tokens) == 1 and check_tokens[0] == -100: seen_bad += 1
            else: seen_good += 1
            if i >= 100: break
        pass

        # Check ratio
        if seen_bad / (seen_bad + seen_good) >= 0.9:
            print(
                "Unsloth: Most labels in your dataset are -100. Training losses will be all 0.\n"\
                "For example, are you sure you used `train_on_responses_only` correctly?\n"\
                "Or did you mask our tokens incorrectly? Maybe this is intended?"
            )
        pass
    pass
pass


def get_max_steps(training_args, n_training_samples, train_dataset):
    # Approximately from https://github.com/huggingface/transformers/blob/main/src/transformers/trainer.py#L2092
    # Determines batch size, max steps, ga etc
    if training_args.world_size > 1:
        raise RuntimeError('Unsloth currently does not support multi GPU setups - but we are working on it!')
    pass

    total_train_batch_size = \
        training_args.per_device_train_batch_size * \
        training_args.gradient_accumulation_steps

    num_update_steps_per_epoch = n_training_samples // training_args.gradient_accumulation_steps
    num_update_steps_per_epoch = max(num_update_steps_per_epoch, 1)
    num_examples = len(train_dataset)

    if training_args.max_steps > 0:
        max_steps = training_args.max_steps
        num_train_epochs = max_steps // num_update_steps_per_epoch + int(
            max_steps % num_update_steps_per_epoch > 0
        )
        num_train_samples = max_steps * total_train_batch_size
    else:
        max_steps = math.ceil(training_args.num_train_epochs * num_update_steps_per_epoch)
        num_train_epochs = math.ceil(training_args.num_train_epochs)
        num_train_samples = num_examples * training_args.num_train_epochs
    return total_train_batch_size, max_steps, num_train_epochs, num_train_samples
pass


def set_training(model):
    # Start training
    model.training = True
    while hasattr(model, "model"):
        model = model.model
        model.training = True
    model.training = True
pass


def unset_training(model):
    # End training
    model.training = False
    while hasattr(model, "model"):
        model = model.model
        model.training = False
    model.training = False
pass


def save_checkpoint(trainer, optimizer, lr_scheduler, args, checkpoint_dir:str, step:int):
    output_dir = os.path.join(checkpoint_dir, f"{PREFIX_CHECKPOINT_DIR}-{step}")
    trainer.save_model(output_dir, _internal_call = False)
    if args.should_save:
        trainer.state.stateful_callbacks["TrainerControl"] = trainer.control.state()
        trainer.state.save_to_json(os.path.join(output_dir, TRAINER_STATE_NAME))
        torch.save(optimizer.state_dict(), os.path.join(output_dir, OPTIMIZER_NAME))
        torch.save(lr_scheduler.state_dict(), os.path.join(output_dir, SCHEDULER_NAME))


from dataclasses import dataclass
@dataclass
class Trainer_Stats:
    metrics: dict
pass


def unsloth_train(trainer, resume_from_checkpoint):
    """
    Unsloth Trainer
    1. Fixes gradient accumulation
    2. Scaled down version of HF's trainer
    3. Much less feature complete
    """
    assert(hasattr(trainer, "args"))
    assert(hasattr(trainer, "model"))
    assert(hasattr(trainer, "train_dataset"))
    assert(hasattr(trainer, "data_collator"))

    training_args = trainer.args
    output_dir = training_args.output_dir
    
    if resume_from_checkpoint is False:
        resume_from_checkpoint = None

    # Load potential model checkpoint
    if isinstance(resume_from_checkpoint, bool) and resume_from_checkpoint:
        resume_from_checkpoint = get_last_checkpoint(output_dir)
        if resume_from_checkpoint is None:
            raise ValueError(f"No valid checkpoint found in output directory ({output_dir})")
        
    if resume_from_checkpoint is not None:
        trainer._load_from_checkpoint(resume_from_checkpoint)

    if resume_from_checkpoint is not None and os.path.isfile(
        os.path.join(resume_from_checkpoint, TRAINER_STATE_NAME)):
        trainer.state = TrainerState.load_from_json(os.path.join(resume_from_checkpoint, TRAINER_STATE_NAME))
        # This line currently does not work as trainer.state does not seem to be updated with the values from the training arguments.
        #trainer.compare_trainer_and_checkpoint_args(training_args, trainer.state)
        trainer._load_callback_state()

    model = trainer.model

    data_collator = trainer.data_collator
    n_training_samples = len(trainer.train_dataset)
    set_training(model)
    transformers_set_seed(training_args.seed)

    if data_collator is None:
        from transformers import DataCollatorForLanguageModeling
        data_collator = DataCollatorForLanguageModeling(
            tokenizer = trainer.tokenizer,
            mlm = False,
            pad_to_multiple_of = 4,
        )
    pass

    # Separate weight decay for parameters
    optimizer_cls, optimizer_kwargs = Trainer.get_optimizer_cls_and_kwargs(training_args)
    decay_parameters = frozenset(Trainer.get_decay_parameter_names(None, model))
    yes_decay, no_decay = [], []
    n_parameters_to_train = 0
    for name, param in model.named_parameters():
        if not param.requires_grad: continue
        if name in decay_parameters: yes_decay.append(param)
        else: no_decay.append(param)
        n_parameters_to_train += param.numel()
    pass
    optimizer_grouped_parameters = [
        {"params" : yes_decay, "weight_decay" : training_args.weight_decay,},
        {"params" : no_decay,  "weight_decay" : 0,}
    ]
    trainable_parameters = \
        optimizer_grouped_parameters[0]["params"] + \
        optimizer_grouped_parameters[1]["params"]
    optimizer = optimizer_cls(optimizer_grouped_parameters, **optimizer_kwargs)

    total_train_batch_size, max_steps, num_train_epochs, num_train_samples = \
        get_max_steps(training_args, n_training_samples, trainer.train_dataset)

    # Get LR scheduler
    lr_scheduler = transformers_get_scheduler(
        name = training_args.lr_scheduler_type,
        optimizer = optimizer,
        num_warmup_steps = training_args.get_warmup_steps(max_steps),
        num_training_steps = max_steps,
        **getattr(training_args, "lr_scheduler_kwargs", {}),
    )

    # Gradient accumulation and grad norm clipping
    max_grad_norm   = training_args.max_grad_norm
    clip_grad_norm_ = torch.nn.utils.clip_grad_norm_
    gradient_accumulation_steps = training_args.gradient_accumulation_steps
    inverse_gradient_accumulation_steps = 1.0 / gradient_accumulation_steps
    inverse_gradient_accumulation_steps = \
        torch.FloatTensor([inverse_gradient_accumulation_steps])\
        .to(device = "cuda:0", non_blocking = True)[0]

    # Mixed precision scaling
    if model.config.torch_dtype == torch.float16:
        mixed_precision = "fp16"
        mixed_dtype = torch.float16
        float16_scaler = torch.cuda.amp.GradScaler()
    else:
        mixed_precision = "bf16"
        mixed_dtype = torch.bfloat16
        float16_scaler = None
    pass
    
    optimizer.zero_grad()

    # torch.cuda.amp.autocast is deprecated >= 2.4
    torch_version = torch.__version__
    if Version(torch_version) < Version("2.4.0"):
        autocast_context_manager = torch.cuda.amp.autocast(
            dtype = mixed_dtype,
            cache_enabled = False,
        )
    else:
        autocast_context_manager = torch.amp.autocast(
            device_type = "cuda",
            dtype = mixed_dtype,
            cache_enabled = False,
        )
    pass

    step = trainer.state.global_step
    accumulated_loss = torch.zeros(1, device = "cuda:0", dtype = torch.float32)[0]
    max_iterations   = int(math.ceil(n_training_samples / gradient_accumulation_steps))
    num_trained_epochs = trainer.state.global_step // max_iterations
    num_trained_steps = step - (max_iterations * num_train_epochs)
    leftover_batches = n_training_samples % gradient_accumulation_steps
    if leftover_batches == 0: leftover_batches = gradient_accumulation_steps

    debug_info = \
        f'==((====))==  Unsloth - 2x faster free finetuning | Num GPUs = {training_args.world_size}\n'\
        f'    \\   /|    Num examples = {n_training_samples:,} | Num Epochs = {num_train_epochs:,}\n'\
        f'O^O/ \\_/ \\    Batch size per device = {training_args.per_device_train_batch_size:,} | Gradient Accumulation steps = {training_args.gradient_accumulation_steps}\n'\
        f'\\        /    Total batch size = {total_train_batch_size:,} | Total steps = {max_steps:,}\n'\
        f' "-____-"     Number of trainable parameters = {n_parameters_to_train:,}'
    logger.warning(debug_info)

    progress_bar = ProgressBar(total = max_steps*num_train_epochs, initial = step, dynamic_ncols = True)
    logging_steps = training_args.logging_steps
    save_strategy = training_args.save_strategy
    save_steps = training_args.save_steps
    # Go through each epoch
    start_time = time.time()
    for epoch in range(num_trained_epochs, num_train_epochs):

        # We also need to shuffle the data loader every epoch!
        transformers_set_seed(training_args.seed + epoch)
        train_dataloader_iterator = iter(torch.utils.data.DataLoader(
            trainer.train_dataset,
            batch_size     = training_args.per_device_train_batch_size,
            sampler        = torch.utils.data.RandomSampler(trainer.train_dataset),
            num_workers    = training_args.dataloader_num_workers,
            collate_fn     = data_collator,
            pin_memory     = training_args.dataloader_pin_memory,
            drop_last      = training_args.dataloader_drop_last,
            worker_init_fn = trainer_utils_seed_worker,
        ))

        
        for j in range(num_trained_steps, max_iterations):
            n_batches = leftover_batches if j == (max_iterations-1) else gradient_accumulation_steps
            batches = [next(train_dataloader_iterator) for j in range(n_batches)]
                
            # Count non zeros before loss calc
            n_items = torch.stack([
                torch.count_nonzero(x["labels"][..., 1:] != -100) for x in batches
            ]).sum()

            # Gradient accumulation
            for batch in batches:
                input_ids = batch["input_ids"].pin_memory().to(device = "cuda:0", non_blocking = True)
                labels    = batch["labels"]   .pin_memory().to(device = "cuda:0", non_blocking = True)

                with autocast_context_manager:
                    loss = model(input_ids = input_ids, labels = labels, n_items = n_items).loss
                    # loss = loss * inverse_gradient_accumulation_steps
                    accumulated_loss += loss.detach()
                pass

                if float16_scaler is None:  loss.backward()
                else: float16_scaler.scale(loss).backward()
            pass

            if float16_scaler is None:
                clip_grad_norm_(trainable_parameters, max_grad_norm)
                optimizer.step()
            else:
                float16_scaler.unscale_(optimizer)
                clip_grad_norm_(trainable_parameters, max_grad_norm)
                float16_scaler.step(optimizer)
                float16_scaler.update()
            lr_scheduler.step()
            optimizer.zero_grad()

            if step % logging_steps == 0:
                progress_bar.write(f"{step}, {round(accumulated_loss.cpu().item(), 4)}")
            pass
            accumulated_loss.zero_()
            progress_bar.update(1)

            step += 1
            if step == max_steps: break

            if save_strategy !="epoch":
                # Should save checkpoints to output directory
                if step % save_steps == 0 and save_steps >= 1 and step != 0:
                    save_checkpoint(trainer, optimizer, lr_scheduler, training_args, checkpoint_dir=output_dir, step = step)
                elif step == int(max_steps * save_steps) and save_steps > 0:
                    save_checkpoint(trainer, optimizer, lr_scheduler, training_args, checkpoint_dir=output_dir, step = step)
        pass

        if save_strategy == "epoch":
            save_checkpoint(trainer, optimizer, lr_scheduler, training_args, checkpoint_dir=output_dir, step = step)

    pass
    progress_bar.close()
    unset_training(model)
    logger.warning("Unsloth: Finished training!")
    end_time = time.time()

    # Return stats
    trainer_stats = Trainer_Stats(metrics = {"train_runtime" : end_time - start_time})
    return trainer_stats
pass
