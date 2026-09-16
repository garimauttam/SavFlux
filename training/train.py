"""
train.py — QLoRA fine-tuning for CodeSage using TRL + PEFT.

PURPOSE
-------
Fine-tuning teaches the model *behaviour and output format* — how CodeSage
structures reviews, cites evidence, and avoids hallucination.  It does NOT
teach repository facts (those stay in ChromaDB/RAG and change every ingest).

DATASET FORMAT  (see data.example.jsonl)
-----------------------------------------
Each line is a JSON object with a "messages" list in ChatML format:
  {"messages": [
      {"role": "system",  "content": "You are CodeSage..."},
      {"role": "user",    "content": "Review this function...\n```python\n...\n```"},
      {"role": "assistant","content": "## Bugs & Critical Issues\n..."}
  ]}

HOW TO RUN
----------
  # 1. Install training dependencies (separate from inference deps)
  pip install -r training/requirements-train.txt

  # 2. Prepare your datasets (split by repo/PR so valid set is unseen)
  #    training/data/train.jsonl   — 80% of examples
  #    training/data/valid.jsonl   — 20% held-out examples

  # 3. Train on a CUDA GPU (A100/H100 recommended; RTX 3090 works for 7B)
  python training/train.py

  # 4. Merge adapter and export to Ollama Modelfile
  python training/train.py --merge --export-ollama

  # 5. Pull merged model into Ollama
  ollama create codesage-7b -f training/output/Modelfile
  # Then set OLLAMA_CHAT_MODEL=codesage-7b in your .env

APPLE SILICON NOTE
------------------
Apple Silicon (MPS) works for inference and small experiments.
For practical QLoRA training use a CUDA GPU or a cloud service
(Google Colab Pro, Lambda Labs, RunPod — ~$1/hr for A10G).

ARCHITECTURE NOTES
------------------
- 4-bit NF4 quantisation via bitsandbytes (QLoRA) halves VRAM vs FP16
- LoRA rank=16 targets q_proj/v_proj — standard for code models
- Learning rate 2e-4 with cosine decay, 3 epochs, batch-size 2 + grad-accum 4
- Max sequence length 2048 tokens — fits a full review prompt with code
"""

import argparse
import json
import os
import sys
from pathlib import Path

# ── Dependency check ──────────────────────────────────────────────────────────
_REQUIRED = ["torch", "transformers", "peft", "trl", "datasets", "bitsandbytes"]

def _check_deps() -> None:
    missing = []
    for pkg in _REQUIRED:
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pkg)
    if missing:
        print(
            f"[train.py] Missing training dependencies: {', '.join(missing)}\n"
            "Install them with:\n"
            "  pip install -r training/requirements-train.txt\n",
            file=sys.stderr,
        )
        sys.exit(1)

_check_deps()

import torch  # noqa: E402 — imports after dep check
from datasets import load_dataset  # noqa: E402
from peft import LoraConfig, TaskType, get_peft_model, PeftModel  # noqa: E402
from transformers import (  # noqa: E402
    AutoModelForCausalLM,
    AutoTokenizer,
    BitsAndBytesConfig,
    TrainingArguments,
)
from trl import SFTTrainer, DataCollatorForCompletionOnlyLM  # noqa: E402

# ── Default paths ─────────────────────────────────────────────────────────────
_TRAINING_DIR = Path(__file__).resolve().parent
_DATA_DIR     = _TRAINING_DIR / "data"
_OUTPUT_DIR   = _TRAINING_DIR / "output"


# ── Hyperparameters ───────────────────────────────────────────────────────────
# Sensible defaults for a 7B coder model on a single A10G/A100 GPU.
# Override via CLI args (see parse_args()).

DEFAULTS = {
    "model_name":       "Qwen/Qwen2.5-Coder-7B-Instruct",
    "train_file":       str(_DATA_DIR / "train.jsonl"),
    "valid_file":       str(_DATA_DIR / "valid.jsonl"),
    "output_dir":       str(_OUTPUT_DIR / "lora-adapter"),
    "merged_dir":       str(_OUTPUT_DIR / "merged"),
    "num_epochs":       3,
    "per_device_batch": 2,
    "grad_accum":       4,           # effective batch = 2 × 4 = 8
    "lr":               2e-4,
    "max_seq_len":      2048,
    "lora_rank":        16,
    "lora_alpha":       32,
    "lora_dropout":     0.05,
    # LoRA target modules for Qwen2.5 — covers attention projection layers.
    # For Llama/Mistral replace with: ["q_proj","v_proj","k_proj","o_proj"]
    "lora_target_modules": ["q_proj", "v_proj", "k_proj", "o_proj", "gate_proj", "up_proj"],
    "load_in_4bit":     True,        # QLoRA — set False for full LoRA on large GPU
    "bf16":             True,        # BF16 on A-series / H-series NVIDIA; set False for older GPUs
}


# ── Tokenizer helper ──────────────────────────────────────────────────────────

def _apply_chat_template(example: dict, tokenizer) -> dict:
    """
    Format a messages list into the model's native chat template.

    TRL's SFTTrainer expects a "text" key containing the full formatted string.
    The tokenizer's apply_chat_template handles role tokens, BOS/EOS, and any
    model-specific formatting (Qwen uses <|im_start|>/<|im_end|>).
    """
    text = tokenizer.apply_chat_template(
        example["messages"],
        tokenize=False,
        add_generation_prompt=False,
    )
    return {"text": text}


# ── Dataset loader ────────────────────────────────────────────────────────────

def load_training_data(train_file: str, valid_file: str, tokenizer):
    """
    Load JSONL datasets and apply the chat template.

    Each example must have a "messages" key (see data.example.jsonl).
    Invalid lines are skipped with a warning — training continues on remaining data.
    """
    if not Path(train_file).exists():
        raise FileNotFoundError(
            f"Training data not found: {train_file}\n"
            "Create training/data/train.jsonl following the format in "
            "training/data.example.jsonl.\n"
            "Aim for 100–500 high-quality accepted review examples."
        )

    dataset = load_dataset(
        "json",
        data_files={
            "train": train_file,
            **({"validation": valid_file} if Path(valid_file).exists() else {}),
        },
        split=None,
    )

    def _safe_apply(example):
        try:
            return _apply_chat_template(example, tokenizer)
        except Exception as exc:
            print(f"[train.py] Warning: skipping malformed example — {exc}")
            return {"text": ""}

    dataset = dataset.map(_safe_apply, remove_columns=["messages"])
    # Drop empty rows (malformed examples that returned "")
    dataset = dataset.filter(lambda ex: len(ex["text"]) > 0)

    print(f"[train.py] Train examples : {len(dataset['train'])}")
    if "validation" in dataset:
        print(f"[train.py] Valid examples : {len(dataset['validation'])}")

    return dataset


# ── Model loader ──────────────────────────────────────────────────────────────

def load_model_and_tokenizer(cfg: dict):
    """
    Load the base model in 4-bit NF4 (QLoRA) and attach a LoRA adapter.

    WHY 4-BIT NF4?
    NormalFloat4 quantisation (Dettmers et al., 2023) lets you fine-tune a
    7B parameter model on a single 24GB GPU (RTX 3090/4090) with near-zero
    quality loss.  Full FP16 fine-tuning of a 7B model needs ~80GB VRAM.

    WHY LoRA?
    Low-Rank Adaptation freezes the base weights and injects small trainable
    matrices into the attention layers.  Only ~0.5% of parameters are updated —
    training is 3–5× faster and the adapter is < 100 MB vs. the 14 GB base.
    """
    tokenizer = AutoTokenizer.from_pretrained(
        cfg["model_name"],
        trust_remote_code=True,
        padding_side="right",   # required for SFTTrainer's packed batching
    )
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    bnb_config = None
    if cfg["load_in_4bit"]:
        bnb_config = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",          # NormalFloat4 — best quality/size
            bnb_4bit_compute_dtype=torch.bfloat16,
            bnb_4bit_use_double_quant=True,     # nested quantisation — extra memory savings
        )

    model = AutoModelForCausalLM.from_pretrained(
        cfg["model_name"],
        quantization_config=bnb_config,
        device_map="auto",          # spread across available GPUs automatically
        trust_remote_code=True,
        torch_dtype=torch.bfloat16 if cfg["bf16"] else torch.float16,
    )
    model.config.use_cache = False   # required when using gradient checkpointing

    lora_config = LoraConfig(
        r=cfg["lora_rank"],
        lora_alpha=cfg["lora_alpha"],
        target_modules=cfg["lora_target_modules"],
        lora_dropout=cfg["lora_dropout"],
        bias="none",
        task_type=TaskType.CAUSAL_LM,
    )
    model = get_peft_model(model, lora_config)
    model.print_trainable_parameters()

    return model, tokenizer


# ── Training ──────────────────────────────────────────────────────────────────

def run_training(cfg: dict) -> None:
    """Fine-tune with SFTTrainer (TRL) using the configured hyperparameters."""
    os.makedirs(cfg["output_dir"], exist_ok=True)

    model, tokenizer = load_model_and_tokenizer(cfg)
    dataset = load_training_data(cfg["train_file"], cfg["valid_file"], tokenizer)

    # Completion-only collator: trains only on the assistant tokens,
    # not on the system/user prompt tokens. This improves efficiency and
    # prevents the model from learning to reproduce the review prompts
    # instead of the review content.
    # The response template is the token that starts the assistant turn
    # in Qwen's ChatML format.
    response_template = "<|im_start|>assistant\n"
    collator = DataCollatorForCompletionOnlyLM(
        response_template=response_template,
        tokenizer=tokenizer,
    )

    training_args = TrainingArguments(
        output_dir=cfg["output_dir"],
        num_train_epochs=cfg["num_epochs"],
        per_device_train_batch_size=cfg["per_device_batch"],
        gradient_accumulation_steps=cfg["grad_accum"],
        learning_rate=cfg["lr"],
        lr_scheduler_type="cosine",
        warmup_ratio=0.05,
        bf16=cfg["bf16"],
        fp16=not cfg["bf16"],
        logging_steps=10,
        save_strategy="epoch",
        evaluation_strategy="epoch" if "validation" in dataset else "no",
        load_best_model_at_end=("validation" in dataset),
        metric_for_best_model="eval_loss",
        greater_is_better=False,
        report_to="none",           # set "wandb" if you want W&B tracking
        gradient_checkpointing=True,
        optim="paged_adamw_8bit",   # 8-bit AdamW — reduces optimizer state memory
    )

    trainer = SFTTrainer(
        model=model,
        args=training_args,
        train_dataset=dataset["train"],
        eval_dataset=dataset.get("validation"),
        tokenizer=tokenizer,
        data_collator=collator,
        dataset_text_field="text",
        max_seq_length=cfg["max_seq_len"],
        packing=False,  # keep False with completion-only collator
    )

    print(f"\n[train.py] Starting training — {cfg['num_epochs']} epoch(s)...")
    trainer.train()

    print(f"[train.py] Saving LoRA adapter to {cfg['output_dir']}")
    trainer.model.save_pretrained(cfg["output_dir"])
    tokenizer.save_pretrained(cfg["output_dir"])
    print("[train.py] Training complete.")


# ── Merge adapter ─────────────────────────────────────────────────────────────

def merge_and_export(cfg: dict) -> None:
    """
    Merge the LoRA adapter into the base model weights and write a full model.

    WHY MERGE?
    Ollama loads full model weights — it cannot apply LoRA adapters at runtime.
    Merging bakes the adapter deltas into the base weights, producing a standard
    HuggingFace model directory that can be converted to GGUF for Ollama.

    After merging, run llama.cpp's convert.py to produce a .gguf file, then
    create an Ollama Modelfile pointing at it.
    """
    adapter_path = cfg["output_dir"]
    merged_path  = cfg["merged_dir"]

    if not Path(adapter_path).exists():
        raise FileNotFoundError(
            f"Adapter not found at {adapter_path}. Run training first."
        )

    os.makedirs(merged_path, exist_ok=True)
    print(f"[train.py] Loading base model {cfg['model_name']} in FP16 for merge...")

    # Load base in FP16 (no quantisation) for the merge step
    base_model = AutoModelForCausalLM.from_pretrained(
        cfg["model_name"],
        torch_dtype=torch.float16,
        device_map="auto",
        trust_remote_code=True,
    )
    tokenizer = AutoTokenizer.from_pretrained(adapter_path, trust_remote_code=True)

    print("[train.py] Merging LoRA adapter into base weights...")
    model = PeftModel.from_pretrained(base_model, adapter_path)
    merged = model.merge_and_unload()

    print(f"[train.py] Saving merged model to {merged_path}")
    merged.save_pretrained(merged_path)
    tokenizer.save_pretrained(merged_path)

    # ── Write Ollama Modelfile ─────────────────────────────────────────────────
    _write_ollama_modelfile(cfg, merged_path)
    print("[train.py] Merge complete.")


def _write_ollama_modelfile(cfg: dict, merged_path: str) -> None:
    """
    Write an Ollama Modelfile that points at the merged model directory.

    After writing this file:
      1. Convert to GGUF:
           python llama.cpp/convert_hf_to_gguf.py training/output/merged --outfile codesage.gguf
      2. Quantise (optional, recommended for CPU inference):
           llama.cpp/llama-quantize codesage.gguf codesage-q4_k_m.gguf Q4_K_M
      3. Register with Ollama:
           ollama create codesage-7b -f training/output/Modelfile
      4. Set OLLAMA_CHAT_MODEL=codesage-7b in .env and restart CodeSage.
    """
    modelfile_path = Path(cfg["output_dir"]).parent / "Modelfile"
    model_name = Path(cfg["model_name"]).name.lower().replace(".", "-")

    modelfile_content = (
        f"# Ollama Modelfile — CodeSage fine-tuned {model_name}\n"
        f"# Generated by training/train.py\n\n"
        f"FROM ./merged\n\n"
        f'SYSTEM """You are CodeSage, a grounded AI code reviewer. '
        f"You only cite evidence present in the provided code. "
        f"You never hallucinate function names, imports, or behaviours that are not shown. "
        f'Structure every review with the required sections."""\n\n'
        f"PARAMETER temperature 0.1\n"
        f"PARAMETER num_predict 4096\n"
        f"PARAMETER stop <|im_end|>\n"
    )
    modelfile_path.write_text(modelfile_content, encoding="utf-8")
    print(f"[train.py] Ollama Modelfile written to {modelfile_path}")
    print(
        "\nNext steps:\n"
        "  1. Install llama.cpp and convert the merged model to GGUF:\n"
        f"       python llama.cpp/convert_hf_to_gguf.py {merged_path} --outfile codesage.gguf\n"
        "  2. (Optional) quantise for CPU/low-VRAM:\n"
        "       llama.cpp/llama-quantize codesage.gguf codesage-q4_k_m.gguf Q4_K_M\n"
        "  3. Register with Ollama:\n"
        f"       ollama create codesage-7b -f {modelfile_path}\n"
        "  4. Update .env:\n"
        "       OLLAMA_CHAT_MODEL=codesage-7b\n"
    )


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="QLoRA fine-tuning for CodeSage",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--model-name",       default=DEFAULTS["model_name"],
                   help="HuggingFace model ID for the base model")
    p.add_argument("--train-file",       default=DEFAULTS["train_file"],
                   help="Path to training JSONL file")
    p.add_argument("--valid-file",       default=DEFAULTS["valid_file"],
                   help="Path to validation JSONL file (optional — skipped if absent)")
    p.add_argument("--output-dir",       default=DEFAULTS["output_dir"],
                   help="Where to save the LoRA adapter")
    p.add_argument("--merged-dir",       default=DEFAULTS["merged_dir"],
                   help="Where to save the merged full model (--merge mode)")
    p.add_argument("--epochs",           type=int,   default=DEFAULTS["num_epochs"])
    p.add_argument("--batch-size",       type=int,   default=DEFAULTS["per_device_batch"])
    p.add_argument("--grad-accum",       type=int,   default=DEFAULTS["grad_accum"])
    p.add_argument("--lr",               type=float, default=DEFAULTS["lr"])
    p.add_argument("--max-seq-len",      type=int,   default=DEFAULTS["max_seq_len"])
    p.add_argument("--lora-rank",        type=int,   default=DEFAULTS["lora_rank"])
    p.add_argument("--no-4bit",          action="store_true",
                   help="Disable 4-bit QLoRA quantisation (requires more VRAM)")
    p.add_argument("--no-bf16",          action="store_true",
                   help="Use FP16 instead of BF16 (for older NVIDIA GPUs)")
    p.add_argument("--merge",            action="store_true",
                   help="Merge the LoRA adapter into the base model after training")
    p.add_argument("--merge-only",       action="store_true",
                   help="Skip training; only merge an existing adapter")
    p.add_argument("--export-ollama",    action="store_true",
                   help="Write an Ollama Modelfile after merging (implies --merge)")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    cfg = {
        "model_name":           args.model_name,
        "train_file":           args.train_file,
        "valid_file":           args.valid_file,
        "output_dir":           args.output_dir,
        "merged_dir":           args.merged_dir,
        "num_epochs":           args.epochs,
        "per_device_batch":     args.batch_size,
        "grad_accum":           args.grad_accum,
        "lr":                   args.lr,
        "max_seq_len":          args.max_seq_len,
        "lora_rank":            args.lora_rank,
        "lora_alpha":           args.lora_rank * 2,
        "lora_dropout":         DEFAULTS["lora_dropout"],
        "lora_target_modules":  DEFAULTS["lora_target_modules"],
        "load_in_4bit":         not args.no_4bit,
        "bf16":                 not args.no_bf16,
    }

    if not args.merge_only:
        run_training(cfg)

    if args.merge or args.merge_only or args.export_ollama:
        merge_and_export(cfg)


if __name__ == "__main__":
    main()
