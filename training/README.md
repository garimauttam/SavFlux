# CodeSage — QLoRA Fine-Tuning

This directory contains the **full** fine-tuning pipeline for CodeSage's local model.
The production request path remains:

```
DeepSeek API  ──►  Prompt + Tools + RAG  ──►  Ollama fallback
```

Fine-tuning teaches **behaviour and output format** — structured reviews, evidence
citation, hallucination avoidance.  Repository facts stay in ChromaDB/RAG and
change on every ingest; they should never be baked into model weights.

---

## Files

| File | Purpose |
|------|---------|
| `train.py` | Full QLoRA training script (TRL + PEFT) |
| `requirements-train.txt` | Training-only pip dependencies |
| `data.example.jsonl` | Single example showing the required JSONL format |
| `data/train.jsonl` | Your training set *(create this — see below)* |
| `data/valid.jsonl` | Held-out validation set *(create this — see below)* |
| `output/lora-adapter/` | Saved LoRA adapter weights after training |
| `output/merged/` | Merged full model for GGUF conversion |
| `output/Modelfile` | Ollama Modelfile pointing at the merged model |

---

## Dataset Format

Each line in `data/train.jsonl` must be a JSON object with a `messages` list
in ChatML format:

```json
{"messages":[
  {"role":"system","content":"You are CodeSage, a grounded code review assistant. Use only the supplied code and evidence."},
  {"role":"user","content":"Review this Python function.\n\n```python\ndef get_user(id):\n    return db.execute(\"SELECT * FROM users WHERE id=\"+id)\n```"},
  {"role":"assistant","content":"## Bugs & Critical Issues\n- **SQL injection** (line 2): `id` is concatenated directly into the query string.\n\n## Suggested Fix\n```python\ndef get_user(id):\n    return db.execute(\"SELECT * FROM users WHERE id=?\", (id,))\n```\n\n## Evidence\nThe string concatenation on line 2 passes unsanitised input to `db.execute`."}
]}
```

Good sources for examples:
- Accepted code reviews from your team's PRs (remove secrets and PII)
- Corrected hallucination examples (before → after fix)
- Tool-use traces from the ReAct review agent (agentic mode)
- Grounded Q&A pairs from RAG chat sessions

Split by **repository or pull request** — validation data must come from
repositories **not seen during training**.

Aim for **100–500 high-quality examples**.  Quality beats quantity.

---

## Quick Start

```bash
# 1. Install training dependencies (separate from server deps)
pip install -r training/requirements-train.txt

# 2. Prepare your data
mkdir -p training/data
# Copy / generate train.jsonl and valid.jsonl into training/data/

# 3. Train (CUDA GPU required for practical speed)
python training/train.py

# 4. Merge adapter + generate Ollama Modelfile
python training/train.py --merge-only --export-ollama

# 5. Convert to GGUF (requires llama.cpp)
python llama.cpp/convert_hf_to_gguf.py training/output/merged --outfile codesage.gguf

# 6. (Optional) quantise for CPU / low-VRAM inference
llama.cpp/llama-quantize codesage.gguf codesage-q4_k_m.gguf Q4_K_M

# 7. Register with Ollama
ollama create codesage-7b -f training/output/Modelfile

# 8. Set the model in .env and restart CodeSage
#    OLLAMA_CHAT_MODEL=codesage-7b
```

---

## CLI Reference

```
python training/train.py --help

  --model-name     HuggingFace model ID  [Qwen/Qwen2.5-Coder-7B-Instruct]
  --train-file     Path to training JSONL
  --valid-file     Path to validation JSONL (optional)
  --output-dir     Where to save the LoRA adapter
  --merged-dir     Where to save the merged full model
  --epochs         Training epochs  [3]
  --batch-size     Per-device batch size  [2]
  --grad-accum     Gradient accumulation steps  [4]  (effective batch = 8)
  --lr             Learning rate  [2e-4]
  --max-seq-len    Max token sequence length  [2048]
  --lora-rank      LoRA rank  [16]
  --no-4bit        Disable QLoRA 4-bit quantisation (needs more VRAM)
  --no-bf16        Use FP16 instead of BF16 (older NVIDIA GPUs)
  --merge          Merge adapter into base weights after training
  --merge-only     Skip training; merge an existing adapter
  --export-ollama  Write Ollama Modelfile after merge
```

---

## Hardware Requirements

| GPU | VRAM | Notes |
|-----|------|-------|
| RTX 3090 / 4090 | 24 GB | Works with 4-bit QLoRA; batch-size 2 |
| A10G | 24 GB | Good price/performance on cloud |
| A100 | 40/80 GB | Fastest; enables larger batch or FP16 |
| Apple Silicon (M1/M2/M3) | Shared | Use `--no-4bit --no-bf16`; training is slow — good for smoke tests only |

For cloud GPU rental: Google Colab Pro+, Lambda Labs, RunPod (~$1–2/hr for A10G).

---

## Evaluation

After training, compare base, base+RAG, and fine-tuned+RAG on the same benchmark:

```bash
# Run the built-in RAG evaluation suite against the production backend
python eval_rag.py

# For a custom benchmark
python eval_rag.py --eval-file your_benchmark.json --top-k 10
```

Key metrics to watch:
- **Hit Rate @ K** — did retrieval surface the right file?
- **Symbol Recall** — did retrieved chunks contain expected function names?
- **Review quality** — human eval: does the fine-tuned model cite evidence and avoid hallucination better than base?

---

## Important Notes

- **Keep secrets out of the dataset.** Scrub API keys, passwords, and personal data before adding any example.
- **Never hard-code repository facts in training data.** Facts (function names, logic) belong in ChromaDB/RAG. Fine-tuning only teaches format and reasoning style.
- **Only deploy after evaluation.** Set `OLLAMA_CHAT_MODEL` to the fine-tuned model only after comparing it against the base model on the benchmark.
