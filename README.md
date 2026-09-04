# Sentinel

An LLM-based incident investigation agent, plus a deliberately broken target application to investigate. Everything runs locally — no API keys, no outbound network calls.

## What's here

- **`agent/`** — Sentinel, a FastAPI service that investigates incidents by collecting evidence from host tooling (`docker logs`, `git log`, `journalctl`, `ss`) and Postgres, stores it in a local vector store, and asks a local LLM (via [Ollama](https://ollama.com)) to reason over it or answer free-form questions.
- **`demo_app/`** — the target: a FastAPI + Postgres + Redis stack, in Docker Compose, with a deliberate mix of intentional faults (wrong DB password, an unhandled `KeyError`, a Redis type error, a flaky simulated payment gateway, plus a background thread emitting realistic log noise) for the agent to find.

## Why

Sentinel doesn't wait for a human to point it at a problem. A background collector keeps polling the target's logs and state in near-real-time, incrementally storing and embedding only what's new, so by the time you ask a question the evidence is already there — grounded in what's actually running, not the model's memory of what should be running.

## Quickstart

Bring up the target stack:

```bash
cd demo_app/lab
docker compose up -d --build
```

Run the agent (Python 3.14, from `agent/`):

```bash
source .venv/bin/activate
pip install -r requirements.txt
ollama serve &                          # separately, if not already running
ollama pull nomic-embed-text
ollama pull qwen3:4b-thinking-2507-q4_K_M   # or any chat model you prefer

SENTINEL_MODEL=qwen3:4b-thinking-2507-q4_K_M uvicorn main:app --reload --port 8001
```

Use a port other than 8000 — the lab API already binds it.

Ask something:

```bash
curl -s 'localhost:8001/chat?q=what+errors+is+the+system+reporting' | jq -r .answer

# or, from the terminal, no service needed:
python ask.py "what errors is the system reporting"
```

## How it fits together

```
demo_app (target)  →  agent/tools/*  →  Evidence  →  SQLite + sqlite-vec  →  retrieval  →  local LLM
                       (docker/git/systemd/db/net)    (chunked + embedded)   (vector KNN)   (Ollama)
```

A background poller (`agent/live_collector.py`) keeps this pipeline fed continuously — delta-fetching new log lines and diffing state snapshots so an idle system doesn't re-embed the same data forever. `/incidents/investigate` triggers a one-shot collection on demand; `/chat` and `ask.py` answer against whatever's already stored.

See [`CLAUDE.md`](CLAUDE.md) for the full architecture, endpoint reference, and the fault catalog in `demo_app`.

## Requirements

- Docker + Docker Compose
- Python 3.14 (or close to it)
- [Ollama](https://ollama.com), running locally, with a chat model and an embedding model pulled
- No test suite or linter is configured; verification is manual, against the running stack.
