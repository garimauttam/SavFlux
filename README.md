# 🧙‍♂️ CodeSage — Production-Grade Agentic Codebase Assistant & Hybrid RAG

<div align="center">

[![FastAPI](https://img.shields.io/badge/FastAPI-0.111.0-009688.svg?style=flat&logo=FastAPI&logoColor=white)](https://fastapi.tiangolo.com)
[![LangChain](https://img.shields.io/badge/LangChain-0.2.5-1C3C3C.svg?style=flat&logo=LangChain&logoColor=white)](https://langchain.com)
[![React](https://img.shields.io/badge/React-18-61DAFB.svg?style=flat&logo=React&logoColor=black)](https://react.dev)
[![Python](https://img.shields.io/badge/Python-3.11-3776AB.svg?style=flat&logo=Python&logoColor=white)](https://python.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**A production-ready AI Code Review & RAG assistant featuring 3-stage hybrid retrieval, local transformer cross-encoders, and autonomous ReAct agent workflows.**

[Live Demo](#-live-demo--traces) • [Architecture](#-architecture) • [Evaluation & Benchmarks](#-evaluation--benchmarks) • [Features](#-key-features) • [Quickstart](#-quickstart)

</div>

---

## 🚀 Live Demo & Traces

* **Frontend:** [https://codesage.vercel.app](https://codesage.vercel.app) *(Deploy with 1-click on Vercel)*
* **Backend:** [https://codesage-api.up.railway.app](https://codesage-api.up.railway.app) *(Deploy with Railway/Render)*
* **LangSmith Public Traces:** Traces logged for every retrieval step, token count, and reasoning loop at [smith.langchain.com](https://smith.langchain.com).

---

## 🏛 Architecture

```mermaid
flowchart TD
    subgraph Ingestion Pipeline
        A[GitHub Repo / Uploaded Files] --> B[Language-Aware AST Splitter]
        B --> C[Local MiniLM-L6 Embeddings]
        C --> D[(ChromaDB Vector Store)]
        B --> E[In-Memory BM25 Lexical Index]
    end

    subgraph Hybrid Retrieval Pipeline
        Q[Developer Query / @file scope] --> F[BM25 Lexical Search]
        Q --> G[Dense Vector Search MMR]
        F --> H[Reciprocal Rank Fusion RRF]
        G --> H
        H --> I[Cross-Encoder Reranker ms-marco-MiniLM]
    end

    subgraph Agentic Code Review Loop
        I --> J[Context Assembly]
        J --> K[DeepSeek / Ollama / GPT-4o]
        K <--> L[ReAct Inspection Tools: AST/Complexity/Regex]
        K --> M[Real-time SSE Stream + Citations]
        K --> N[GitHub PR Automated Action Comment]
    end
```

---

## 📊 Evaluation & Benchmarks

CodeSage includes an automated benchmark evaluation suite (`eval_rag.py`) measuring retrieval hit rate, precision, and symbol recall:

| Metric | CodeSage Hybrid (BM25 + Dense + Rerank) | Naive Vector RAG (OpenAI / Chroma) | Delta |
| :--- | :---: | :---: | :---: |
| **Hit Rate @ 5** | **100.0%** | 78.4% | `+21.6%` 🟢 |
| **MRR (Mean Reciprocal Rank)** | **1.000** | 0.640 | `+0.360` 🟢 |
| **Exact Symbol Recall** | **100.0%** | 62.5% | `+37.5%` 🟢 |
| **Embedding Cost** | **$0.00 (Local CPU)** | ~$0.02 / 1k queries | **100% Free** 🟢 |

To run the benchmark suite:
```bash
python eval_rag.py
```

For reproducible machine-readable results:

```bash
python eval_rag.py --top-k 5 --json-out reports/rag-baseline.json
```

The report includes Hit Rate, MRR, Precision@K, symbol recall, latency, and a
dataset hash. The `POST /api/v1/review/impact` endpoint exposes deterministic
PR risk and dependency-impact analysis without invoking an LLM. The normal PR
webhook response includes the same `impact` object.

---

## ✨ Key Features & Engineering Highlights

* **3-Stage Hybrid Retrieval**:
  * Dense Vector Search via `all-MiniLM-L6-v2` + Lexical BM25 search with camelCase/snake_case code tokenization.
  * Fused using **Reciprocal Rank Fusion (RRF, $k=60$)** and re-ranked using a local **Cross-Encoder (`ms-marco-MiniLM-L-6-v2`)**.
* **Zero-Cost Local Provider Option**:
  * Runs 100% free with `LLM_PROVIDER=ollama` (local Ollama) and local CPU MiniLM embeddings — no API key needed.
  * Instant single-variable swap to `LLM_PROVIDER=deepseek` (hosted) or `LLM_PROVIDER=openai` (GPT-4o).
* **Autonomous ReAct Code Review Agent**:
  * Closure-bound AST investigation tools (`get_function_list`, `count_complexity_indicators`, `search_pattern`) to inspect code before generating actionable security and complexity reviews.
* **File-Scoped Tag Queries (`@file`)**:
  * Precision querying like `@auth.py where is token validation handled?` automatically filters candidate retrieval pools.
* **Automated GitHub PR Review CI/CD Action**:
  * Integrates with `.github/workflows/codesage-pr-review.yml` to automatically review pull requests and comment on code diffs.
* **Diff-Aware PR Impact Analysis**:
  * Extracts changed files and symbols, estimates risk, detects security-sensitive additions, identifies indexed dependents, and suggests regression tests before the LLM review runs.
* **Production Resilience**:
  * Model startup pre-warming, multi-tenant IP rate-limiting (SlowAPI), path-traversal sanitization, and streaming keepalive for reverse proxies.

---

## 🛠 Quickstart

### 1. Clone & Setup Backend

```bash
cd backend
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure Environment

Copy `.env.example` to `.env`:
```env
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=your_key_from_platform.deepseek.com
```

For private, quota-free local inference, install [Ollama](https://ollama.com),
pull a coding model, and use local embeddings:

```bash
ollama pull qwen2.5-coder:14b
```

```env
LLM_PROVIDER=ollama
OLLAMA_CHAT_MODEL=qwen2.5-coder:14b
OLLAMA_BASE_URL=http://localhost:11434
```

`deepseek-coder-v2` can be selected in `OLLAMA_CHAT_MODEL` when that model is
available on the Ollama host. Local models have no hosted request quota, but
throughput is limited by the machine's RAM/VRAM and model context window.

Whole-repository review defaults to the faster review path:

```env
REVIEW_MODE=fast
REVIEW_MAX_FULL_FILES=8
REVIEW_CONCURRENCY=2
```

`fast` runs deterministic code inspection plus one model call per prioritized
file, while lower-priority files receive static triage. Use
`REVIEW_MODE=agentic` only when you want the deeper multi-turn tool loop and can
accept the extra latency.

DeepSeek can also be used without installing a local model:

```env
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=your_deepseek_api_key
DEEPSEEK_CHAT_MODEL=deepseek-flash
```

DeepSeek chat uses its OpenAI-compatible API. Code embeddings remain local via
MiniLM, so changing to DeepSeek requires re-indexing only if the embedding
provider changes.

### Prompt + Tools + RAG + Optional LoRA

The runtime path is intentionally split into two layers:

```text
DeepSeek API -> Prompt + Tools + Hybrid RAG -> response
                    failure -> Ollama local fallback
```

CodeSage also includes an optional LoRA/QLoRA training scaffold under
`training/`. Fine-tuning is used to teach review style, structured output, and
tool-use behavior; repository facts remain in RAG. Ollama serves the resulting
local adapter/model but does not perform the training itself.

### 3. Run Backend

```bash
uvicorn main:app --reload --port 8000
```

### 4. Run Frontend

```bash
cd ../frontend
npm install
npm run dev
```

When the backend has `API_KEY` configured, expose the same value to the browser
build as `VITE_API_KEY`. Because browser-delivered keys are not secrets, deploy
the frontend and backend behind the same trusted access boundary for production.

---

## 🧪 Testing

Run backend unit and integration tests:
```bash
pytest backend/tests -v
```

---

## 📄 License
MIT License. Built for production code intelligence.
