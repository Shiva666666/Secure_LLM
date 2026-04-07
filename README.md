# LLM Semantic Firewall Proxy

Prompt injection mitigation using **embedding similarity** and **Supabase (pgvector)**. A FastAPI middleware sits between clients and a local LLM (LM Studio): it embeds the user prompt, compares it to stored malicious patterns, blocks high-similarity requests, and otherwise forwards OpenAI-compatible chat (including streaming) to LM Studio.

## Stack

- FastAPI, Uvicorn, httpx, supabase-py, python-dotenv, sse-starlette
- LM Studio: BGE-small (384-d) for embeddings + any chat model for completions
- Supabase RPC: `match_malicious_patterns(match_count, match_threshold, query_embedding)`

## Setup

1. Copy `.env.example` to `.env` and set `SUPABASE_URL`, `SUPABASE_KEY`, `LM_STUDIO_URL` (must end with `/v1`), `EMBEDDING_MODEL_NAME`.
2. `pip install -r requirements.txt`
3. Run LM Studio with embedding and chat models loaded; start server: `uvicorn main:app --host 0.0.0.0 --port 8000`
4. Chat UI: `http://localhost:8000` — API: `POST /v1/chat/completions`

## Security note

Do not commit `.env` or real API keys. Rotate any key that was ever committed publicly.
