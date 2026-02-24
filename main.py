"""
LLM Semantic Firewall Proxy: FastAPI middleware that blocks prompts semantically
similar to known malicious patterns (Supabase + 384-d BGE embeddings via LM Studio).
"""

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from sse_starlette.sse import EventSourceResponse
from supabase import create_client, Client

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration (validated on import)
# ---------------------------------------------------------------------------
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
LM_STUDIO_URL = os.getenv("LM_STUDIO_URL", "http://localhost:1234/v1").rstrip("/")
EMBEDDING_MODEL_NAME = os.getenv("EMBEDDING_MODEL_NAME")

REQUIRED = ["SUPABASE_URL", "SUPABASE_KEY", "EMBEDDING_MODEL_NAME"]
for var in REQUIRED:
    if not os.getenv(var):
        raise RuntimeError(f"Missing required env var: {var}. Copy .env.example to .env and set values.")

# Supabase RPC contract: match_malicious_patterns(match_count, match_threshold, query_embedding)
# returns rows with a 'similarity' or 'score' column; we use max over returned rows.
SIMILARITY_THRESHOLD = 0.85
RPC_MATCH_THRESHOLD = 0.0  # return all matches above this; we take max and compare to SIMILARITY_THRESHOLD in Python
RPC_MATCH_COUNT = 20
PROMPT_LOG_TRUNCATE = 100

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="LLM Semantic Firewall Proxy")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

STATIC_DIR = Path(__file__).resolve().parent / "static"


@app.get("/")
async def root():
    """Serve the chat UI."""
    index = STATIC_DIR / "index.html"
    if not index.exists():
        return JSONResponse(status_code=404, content={"detail": "Chat UI not found"})
    return FileResponse(index)


def _last_message_content(messages: list[dict[str, Any]]) -> str:
    """Extract content from the last message. Supports string or list (multimodal: use first text part)."""
    if not messages:
        return ""
    last = messages[-1]
    content = last.get("content")
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                return (part.get("text") or "").strip()
        return ""
    return str(content).strip()


async def _get_embedding(prompt: str) -> list[float]:
    """Call LM Studio /v1/embeddings; returns 384-dim vector. Raises on error."""
    url = f"{LM_STUDIO_URL}/embeddings"
    payload = {"model": EMBEDDING_MODEL_NAME, "input": prompt}
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(url, json=payload)
    if resp.status_code != 200:
        logger.warning("LM Studio embeddings returned status=%s body=%s", resp.status_code, resp.text[:200])
        raise RuntimeError("Embedding service unavailable; is LM Studio running?")
    data = resp.json()
    if isinstance(data, dict) and "error" in data and not data.get("data"):
        err_msg = data.get("error") or data.get("message") or "Unknown error"
        if isinstance(err_msg, dict):
            err_msg = err_msg.get("message") or str(err_msg)
        logger.warning("LM Studio embeddings returned error payload: %s", err_msg)
        raise RuntimeError(f"Embedding service error: {err_msg}")
    emb = data.get("data", [{}])[0].get("embedding") if isinstance(data, dict) else None
    if not emb or len(emb) != 384:
        raise RuntimeError("Invalid embedding response (expected 384 dimensions)")
    return emb


def _max_similarity(query_embedding: list[float]) -> float:
    """Call Supabase RPC match_malicious_patterns; returns max similarity (0 if no rows)."""
    payload = {"match_count": RPC_MATCH_COUNT, "match_threshold": RPC_MATCH_THRESHOLD, "query_embedding": query_embedding}
    try:
        result = supabase.rpc("match_malicious_patterns", payload).execute()
    except Exception as e:
        logger.error("Supabase RPC error: %s", e)
        raise RuntimeError("Security check unavailable") from e
    rows = result.data if hasattr(result, "data") else []
    if not rows:
        return 0.0
    # Support either 'similarity' or 'score' column
    scores = []
    for row in rows:
        if isinstance(row, dict):
            s = row.get("similarity") or row.get("score")
            if s is not None:
                scores.append(float(s))
    return max(scores) if scores else 0.0


async def _forward_stream(lm_url: str, body: dict[str, Any]):
    """Stream LM Studio response as SSE without buffering. Yields payloads for EventSourceResponse."""
    async with httpx.AsyncClient(timeout=120.0) as client:
        async with client.stream("POST", lm_url, json=body) as response:
            if response.status_code != 200:
                text = await response.aread()
                raise RuntimeError(f"LM Studio returned {response.status_code}: {text.decode()[:300]}")
            async for line in response.aiter_lines():
                if line and line.startswith("data:"):
                    payload = line[5:].strip()
                    yield {"data": payload}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """OpenAI/LM Studio compatible endpoint: embed prompt -> RPC similarity -> block or forward."""
    try:
        body = await request.json()
    except Exception as e:
        logger.warning("Invalid JSON body: %s", e)
        return JSONResponse(status_code=400, content={"error": "Invalid JSON body"})

    messages = body.get("messages")
    if not messages:
        return JSONResponse(status_code=400, content={"error": "messages array is required and must be non-empty"})

    prompt = _last_message_content(messages)
    if not prompt:
        return JSONResponse(status_code=400, content={"error": "Last message has no text content"})

    # --- Step A: Embedding ---
    try:
        embedding = await _get_embedding(prompt)
    except Exception as e:
        logger.warning("Embedding failed: %s", e)
        return JSONResponse(
            status_code=503,
            content={"error": "LM Studio unavailable", "detail": str(e)},
        )

    # --- Step B: Vector search (run sync Supabase call in thread) ---
    try:
        max_sim = await asyncio.to_thread(_max_similarity, embedding)
    except RuntimeError as e:
        logger.error("Security check failed: %s", e)
        return JSONResponse(
            status_code=503,
            content={"error": "Security check unavailable", "detail": str(e)},
        )

    # --- Step C: Decision ---
    log_prompt = (prompt[:PROMPT_LOG_TRUNCATE] + "…") if len(prompt) > PROMPT_LOG_TRUNCATE else prompt
    if max_sim > SIMILARITY_THRESHOLD:
        logger.info("[BLOCK] Score: %.2f | Prompt: \"%s\"", max_sim, log_prompt)
        return JSONResponse(
            status_code=403,
            content={
                "error": "Security Block: Adversarial intent detected.",
                "similarity_score": round(max_sim, 4),
            },
        )

    logger.info("[ALLOW] Score: %.2f | Prompt: \"%s\"", max_sim, log_prompt)

    # --- Forward to LM Studio ---
    lm_url = f"{LM_STUDIO_URL}/chat/completions"
    stream = body.get("stream", False)

    try:
        if stream:
            return EventSourceResponse(_forward_stream(lm_url, body))
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(lm_url, json=body)
        if resp.status_code != 200:
            return JSONResponse(status_code=resp.status_code, content=resp.json() if resp.content else {})
        return JSONResponse(content=resp.json())
    except httpx.ConnectError as e:
        logger.warning("LM Studio connection error: %s", e)
        return JSONResponse(
            status_code=503,
            content={"error": "LM Studio unavailable", "detail": "Connection failed. Is LM Studio running?"},
        )
    except Exception as e:
        logger.exception("Forward error: %s", e)
        return JSONResponse(
            status_code=503,
            content={"error": "LM Studio unavailable", "detail": str(e)},
        )
