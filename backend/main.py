"""
main.py — FastAPI application entry point.

This file's ONLY job is to:
1. Create the FastAPI app instance
2. Add middleware (CORS, etc.)
3. Mount the routers
4. Run health checks

It should NOT contain any business logic.
"""

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager

# Silence ChromaDB telemetry before any chromadb import.
# ChromaDB's posthog telemetry library has a signature mismatch that spams
# "capture() takes 1 positional argument but 3 were given" on every DB call,
# drowning real log output. Setting this env var disables the telemetry client.
os.environ["ANONYMIZED_TELEMETRY"] = "False"
os.environ["CHROMA_TELEMETRY"] = "false"

# Chroma 0.5.0 can still call the installed PostHog client even when
# anonymized_telemetry=False. The PostHog API changed its capture signature,
# which produces noisy warnings on every Chroma operation. Make the optional
# telemetry sink a no-op before any Chroma client is constructed.
try:
    import posthog
    posthog.disabled = True
    posthog.capture = lambda *args, **kwargs: None
except Exception:
    pass

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from app.limiter import limiter

from app.core.config import get_settings
from app.api import ingest, chat, review, write, metrics, agent, prompts, analytics, snippets, activity, bulk, file_tree, diff, notifications, slash

logger = logging.getLogger(__name__)

settings = get_settings()

# ── LangSmith tracing ─────────────────────────────────────────────────────────
# LangSmith auto-instruments every LangChain call when these env vars are set.
# We read them from our Pydantic settings (which may come from .env or Railway vars)
# and push them into os.environ so the LangChain SDK picks them up automatically.
#
# WHY os.environ AND NOT JUST THE SETTINGS OBJECT?
# LangChain's tracer reads from os.environ directly at import time, not from our
# settings object. We have to set them before any langchain import runs at request
# time — doing it here at app startup guarantees that ordering.
#
# If the vars aren't set, this block is a no-op. Tracing is purely opt-in.
if settings.langchain_tracing_v2:
    os.environ["LANGCHAIN_TRACING_V2"] = settings.langchain_tracing_v2
if settings.langchain_api_key:
    os.environ["LANGCHAIN_API_KEY"] = settings.langchain_api_key
if settings.langchain_project:
    os.environ["LANGCHAIN_PROJECT"] = settings.langchain_project

logger.info(
    "LangSmith tracing: %s",
    "ENABLED (project: %s)" % settings.langchain_project
    if settings.langchain_tracing_v2 == "true"
    else "disabled",
)

# ── Rate limiter ──────────────────────────────────────────────────────────────
# The shared limiter instance lives in app/limiter.py — imported above.
# All routes (chat, review) import from that same module, so they all share
# one counter store per IP. A user cannot bypass limits by switching endpoints.


# ── Lifespan — replaces deprecated @app.on_event("startup") ──────────────────
# FastAPI 0.93+ recommends lifespan context managers over @app.on_event.
# Everything before `yield` runs at startup; everything after runs at shutdown.
# The @asynccontextmanager decorator makes a regular async generator into a
# context manager that FastAPI knows how to call.
#
# WHY WARM UP THE RERANKER HERE?
# The cross-encoder is 80MB. On first use it downloads from HuggingFace and
# loads into memory (~2-4s). Warming it up at startup means the first real
# request is instant — the model is already hot.
@asynccontextmanager
async def lifespan(app: FastAPI):
    async def warm_models():
        try:
            from app.services.reranker import _get_cross_encoder
            from app.services.llm_factory import get_embedding_fn
            logger.info("Warming up models (reranker + embedding)...")
            await asyncio.to_thread(_get_cross_encoder)
            await asyncio.to_thread(get_embedding_fn)
            logger.info("Models warm and ready.")
        except Exception as e:
            logger.warning(f"Model warmup skipped: {e}")

    async def warm_bm25():
        try:
            from app.services.retrieval_service import _get_vectorstore, _get_bm25_index
            vs = await asyncio.to_thread(_get_vectorstore)
            await asyncio.to_thread(_get_bm25_index, vs)
            logger.info("BM25 index warmed.")
        except Exception as e:
            logger.warning(f"BM25 warmup skipped: {e}")

    # Do not block socket binding on model downloads or a large BM25 rebuild.
    # Requests can arrive immediately and pay the warmup cost only if needed.
    warmup_tasks = [asyncio.create_task(warm_models()), asyncio.create_task(warm_bm25())]

    # ── P1 #6 File watcher — background incremental re-index ─────────────────
    try:
        from app.services.watcher_service import start_watcher_background
        watcher_task = asyncio.create_task(start_watcher_background())
    except Exception:
        watcher_task = None

    yield   # ← server is live and handling requests here

    # Stop watcher
    try:
        from app.services.watcher_service import stop_watcher_background
        await stop_watcher_background()
    except Exception:
        pass
    if 'watcher_task' in locals() and watcher_task:
        watcher_task.cancel()
        try:
            await watcher_task
        except Exception:
            pass
    for task in warmup_tasks:
        task.cancel()
    await asyncio.gather(*warmup_tasks, return_exceptions=True)

    # ── Shutdown — persist BM25 index to disk ─────────────────────────────────
    # Saving here ensures the next cold start skips the expensive rebuild.
    # This runs even on graceful SIGTERM (Railway rolling deploys, etc.).
    try:
        from app.services.retrieval_service import save_bm25_on_shutdown
        await asyncio.to_thread(save_bm25_on_shutdown)
        logger.info("BM25 index saved to disk on shutdown.")
    except Exception as e:
        logger.warning(f"BM25 shutdown save skipped: {e}")


app = FastAPI(
    title="CodeSage API",
    description="RAG-powered codebase Q&A and code review assistant",
    version="1.0.0",
    lifespan=lifespan,          # pass the lifespan context manager here
)

# Attach limiter to app state — route files import `app.state.limiter` via the
# `@limiter.limit(...)` decorator
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

# ── Request size limit ────────────────────────────────────────────────────────
# Without this, a malicious client could POST a multi-gigabyte body and exhaust
# server memory. 10MB covers any reasonable code upload.
# This runs before route handlers, so it's the first line of defence.
@app.middleware("http")
async def limit_request_size(request: Request, call_next):
    max_body_size = 10 * 1024 * 1024  # 10 MB
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > max_body_size:
        return JSONResponse(
            status_code=413,
            content={"detail": "Request body too large. Maximum size is 10MB."},
        )
    return await call_next(request)


@app.middleware("http")
async def analytics_middleware(request: Request, call_next):
    """P2 Analytics Trends — record per-request latency for history sparklines (60s throttle, $0)."""
    start = time.time()
    response = await call_next(request)
    try:
        latency_ms = (time.time() - start) * 1000
        # Skip health and analytics endpoints themselves to reduce noise
        path = request.url.path
        if path not in ("/health", "/api/v1/metrics/history"):
            from app.services.analytics_service import record_latency
            record_latency(path, latency_ms, response.status_code)
    except Exception:
        pass
    return response


# ── CORS ───────────────────────────────────────────────────────────────────────
# CORS = Cross-Origin Resource Sharing.
# Without this, the browser blocks requests from localhost:3000 (React dev server)
# to localhost:8000 (FastAPI). It's a browser security feature, not a server one.
# We whitelist only our known frontend origins — never use allow_origins=["*"] in prod.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Routers ────────────────────────────────────────────────────────────────────
# /api/v1/ prefix on all routes — good practice for API versioning
# If you ever need breaking changes, you add /api/v2/ without removing v1
app.include_router(ingest.router, prefix="/api/v1")
app.include_router(chat.router, prefix="/api/v1")
app.include_router(review.router, prefix="/api/v1")
app.include_router(write.router, prefix="/api/v1")
app.include_router(metrics.router, prefix="/api/v1")
app.include_router(agent.router, prefix="/api/v1")
app.include_router(prompts.router, prefix="/api/v1")
app.include_router(snippets.router, prefix="/api/v1")
app.include_router(activity.router, prefix="/api/v1")
app.include_router(bulk.router, prefix="/api/v1")
app.include_router(file_tree.router, prefix="/api/v1")
app.include_router(diff.router, prefix="/api/v1")
app.include_router(notifications.router, prefix="/api/v1")
app.include_router(slash.router, prefix="/api/v1")


@app.get("/health")
async def health_check():
    """
    Deep health check — tests actual dependencies, not just "is the process alive".

    Provider-aware: checks the configured LLM provider (Ollama or hosted
    OpenAI-compatible endpoint) + ChromaDB.
    Railway uses this to decide whether to route traffic to the instance.
    Returns 503 if any check fails — so a misconfigured deploy is caught immediately.
    """
    from app.services.llm_factory import get_hosted_display_name, get_provider_name
    checks: dict[str, str] = {}
    overall_ok = True

    # ── Check 1: LLM provider ─────────────────────────────────────────────────
    # Supported providers: "ollama" | "openai_compatible"  (see config.py Literal)
    if settings.llm_provider == "ollama":
        try:
            import httpx
            async with httpx.AsyncClient(
                base_url=settings.ollama_base_url,
                timeout=5.0,
            ) as client:
                response = await client.get("/api/tags")
            response.raise_for_status()
            checks["llm"] = f"ok (Ollama — {settings.ollama_chat_model})"
        except Exception as e:
            checks["llm"] = f"error: {str(e)[:120]}"
            overall_ok = False
    elif settings.llm_provider == "openai_compatible":
        # Generic OpenAI-style endpoint: Groq, OpenRouter, DeepSeek, Together,
        # HuggingFace, GitHub Models, LM Studio, vLLM... The transport client is
        # openai.AsyncOpenAI because they all speak the OpenAI HTTP API.
        try:
            import openai
            client = openai.AsyncOpenAI(
                api_key=settings.compat_api_key,
                base_url=settings.compat_base_url,
            )
            await client.models.list()
            checks["llm"] = (
                f"ok ({get_hosted_display_name()} — {settings.compat_chat_model})"
            )
        except Exception as e:
            checks["llm"] = f"error: {str(e)[:120]}"
            overall_ok = False
    else:
        checks["llm"] = f"error: unknown LLM_PROVIDER={settings.llm_provider!r}"
        overall_ok = False

    # ── Check 2: ChromaDB ─────────────────────────────────────────────────────
    try:
        import chromadb
        from chromadb.config import Settings as ChromaSettings
        client = chromadb.PersistentClient(
            path=settings.chroma_persist_directory,
            settings=ChromaSettings(anonymized_telemetry=False),
        )
        client.heartbeat()
        checks["chromadb"] = "ok"
    except Exception as e:
        checks["chromadb"] = f"error: {str(e)[:120]}"
        overall_ok = False

    status_code = 200 if overall_ok else 503
    return JSONResponse(
        status_code=status_code,
        content={
            "status": "ok" if overall_ok else "degraded",
            "service": "CodeSage API",
            "provider": get_provider_name(),
            "checks": checks,
        },
    )


# ── Dev server entrypoint ──────────────────────────────────────────────────────
# This block only runs when you execute `python main.py` directly.
# In production (Docker/Railway), uvicorn is started by the Dockerfile CMD instead.
if __name__ == "__main__":
    import uvicorn
    # Read port from settings — Railway injects PORT env var, local dev uses 8000
    uvicorn.run("main:app", host="0.0.0.0", port=settings.port, reload=True)
