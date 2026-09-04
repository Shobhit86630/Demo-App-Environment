# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## What this is

Two halves of an incident-investigation demo:

- `agent/` — **Sentinel**, a FastAPI service that investigates incidents by shelling out to host tooling (docker, git, journalctl, ss) and querying Postgres, then returns structured evidence.
- `demo_app/` — **the target**. A deliberately broken FastAPI + Postgres + Redis stack in Docker Compose whose stated purpose ("produce errors and technical glitches") is to give the agent something real to diagnose.

`demo_app` is a separately-versioned git repo (tracked by the parent as a gitlink at commit `aa5e618`, with no `.gitmodules`). Committing inside `demo_app` does not update the parent pointer — `git add demo_app` in the parent does. The parent repo has no remote; `demo_app`'s remote is `github.com/Shobhit86630/Demo-App-Environment`.

## Commands

Bring up the target stack (from `demo_app/lab/`):

```bash
docker compose up -d --build
docker compose logs -f api
docker compose down -v          # -v also drops the postgres volume
```

Run the agent (from `agent/`, Python 3.14 venv):

```bash
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload --port 8001
```

**Use a port other than 8000** — the lab API already binds 8000 on the host, and `agent/main.py` and `demo_app/lab/api/main.py` are both `main:app`, so uvicorn's defaults collide in both dimensions.

There is no test suite, linter, or formatter configured. Verification is manual: hit the endpoints (`curl localhost:8001/incidents/investigate | jq`) with the lab stack up.

## Architecture

**Evidence pipeline** (`agent/`): `main.py` exposes each tool as its own debug endpoint (`/tools/docker/logs`, `/tools/database`, …) *and* exposes `/incidents/investigate`, which calls `collect_incident_evidence()`. That collector runs four tools in sequence, wraps each result in an `Evidence` dataclass (`evidence.py`), and returns `[Evidence.to_dict()]` — a timestamp is stamped at serialization time, not at collection time.

Two invariants hold this together, and new tools must follow both:

1. **Every tool returns a dict with a `success` boolean**, plus its payload and an `error` key that is `None` on success. Tools never raise — subprocess failures and DB exceptions are caught and reported as `success: False`. The collector reads `success` to decide `severity` (`INFO` vs `ERROR`), so a tool that raises breaks the whole investigation instead of degrading one piece of evidence.
2. **Anything touching the host is allowlisted and bounded.** `docker.py` and `systemd.py` reject container/service names outside a hardcoded set and clamp `lines` to 1–1000; `git.py` clamps `limit` to 1–50 and pins `-C` to a hardcoded repo path. All subprocess calls use list-form args with a 10s timeout — never `shell=True`, never interpolated strings.

**Host coupling.** The agent runs on the host, not in a container: it reads Docker container logs by name (`sentinel-api`, `sentinel-postgres`, `sentinel-redis` — the `container_name` values in `docker-compose.yml`), connects to Postgres on the *published* port `5433`, and reads `journalctl -u docker` and `ss -tulpn`. Renaming a container in the compose file silently breaks `docker.py`'s allowlist, and `git.py`'s `REPOSITORY` constant is an absolute path to this checkout.

**The lab's faults are intentional.** `docker-compose.yml` sets `DB_PASSWORD: wrong_password` while Postgres is initialized with `sentinel`, so `GET /db` on the lab API fails authentication by design. The lab endpoints also have no error handling — `psycopg2.connect` and `redis.ping()` raise straight through to a 500, which is the point. Do not "fix" these unless asked; they are the incidents the agent exists to find.

## Working pipeline

`.Codex/agents/` defines three project subagents that fan out from the main session:

| Agent | Model | Access | Job |
|---|---|---|---|
| `explore` | sonnet | read-only | Locate the relevant code and report constraints, with `file:line` anchors |
| `build` | opus | read/write | Implement the scoped change; never commits |
| `review` | opus | read-only | Adversarial pass over the working-tree diff before tests |

`/pipeline <task>` runs a task through all three and then validates. **Validation stays with the main session** — it reads the diff, brings up the lab stack, and hits the endpoints itself rather than trusting a subagent's self-report. Subagents start cold, so each prompt has to restate the task in full.

## Reasoning layer

`agent/reasoning.py` is the LLM stage that runs *after* `collect_incident_evidence()`. It posts the collected evidence to a **local Ollama daemon** (`http://localhost:11434`) and asks a Llama model for a root-cause reading. There are no API keys anywhere in this project and no outbound calls — if Ollama is not running, nothing reaches the network.

It uses stdlib `urllib` only, so `requirements.txt` is unchanged.

Two environment variables steer it, both read at import time:

- `SENTINEL_MODEL` — defaults to `llama3.1:8b`. Sized to fit a 6GB GPU; `llama3.3` is 70B/39.6GB and will not load on this machine.
- `OLLAMA_HOST` — defaults to `http://localhost:11434`. Point it at another box to run a larger model remotely. That endpoint is unauthenticated, so only do that on a trusted network.

`analyze_incident()` holds the same contract as the tools: it returns a dict with `success`, `analysis`, `raw`, and an `error` that is `None` on success, and it never raises. A stopped daemon or an unpulled model degrades the `analysis` key to `success: False` with an actionable error while the evidence still returns intact. `TIMEOUT` is 180s, deliberately not the 10s bound the subprocess tools use, because local inference is slow. Each evidence item's `raw_data` is truncated to `MAX_RAW_CHARS` (3000) so one noisy log dump cannot evict the other evidence from the context window.

`GET /incidents/investigate` runs the reasoning pass by default and returns it under an `analysis` key; `?analyze=false` skips it and returns evidence immediately. `GET /tools/llm/health` reports whether the daemon is reachable and the model is pulled.

## RAG pipeline

The full flow is `sources -> evidence -> store -> retrieval -> LLM`:

| Stage | File | What it does |
|---|---|---|
| parse | `agent/evidence_collector.py` | Wraps each tool result as `Evidence`. Severity is three-state: `ERROR` on failure, `WARNING` when a source succeeds but returns nothing, `INFO` otherwise |
| store | `agent/store.py` | SQLite + `sqlite-vec`. Persists evidence at full fidelity, chunks it (800 chars, 100 overlap), embeds each chunk and indexes the vectors in a `vec0` virtual table |
| embed | `agent/embeddings.py` | `nomic-embed-text` via Ollama, 768-dim, `keep_alive: 0s` |
| retrieve | `agent/retrieval.py` | Embeds the query, KNN over the vector index, re-ranks with severity, fills a character budget |
| reason | `agent/reasoning.py` | `llama3.1:8b` via Ollama |

**The store is SQLite, deliberately not the lab's Postgres.** The agent must not depend on the system it diagnoses — when Postgres is the incident, a Postgres-backed store fails exactly when it is needed. `SENTINEL_DB` overrides the path; the file is `agent/sentinel.db`.

**Retrieval degrades rather than failing.** No embedding model or a stopped daemon drops it to lexical scoring over the same stored chunks; `mode` in the response says which path ran and `degraded_reason` says why.

**Empty sources stay visible.** A source that returns nothing produces no chunks, so it would vanish from a chunk-only prompt. `format_context()` leads with an inventory of every source consulted, marking the ones that returned no data — that absence is itself evidence.

**Sizing is bound by a 6GB GPU.** `keep_alive: 0s` unloads the embedder so the reasoning model gets the whole card; a 12000-char budget pushed `llama3.1:8b` past 180s, so the budget is 6000 and the timeout is 300s. Embedding 54 chunks takes ~45s, retrieval is instant, reasoning is ~90-245s. That is why `?analyze=false` exists.

## Talking to the LLM

Four ways in, from most to least grounded in evidence:

```bash
# 1. Ask about stored evidence from the terminal (RAG, no service needed)
cd agent && source .venv/bin/activate
python ask.py "why can't the api reach the database?"
python ask.py                        # interactive loop

# 2. Same thing over HTTP
curl -s 'localhost:8001/chat?q=which+sources+reported+errors' | jq -r .answer

# 3. Full investigation: collect, store, embed, retrieve, reason
curl -s 'localhost:8001/incidents/investigate?query=database+auth' | jq .analysis

# 4. Raw model, no evidence at all
ollama run llama3.1:8b
```

`/chat` and `ask.py` reuse whatever the last investigation stored — they retrieve and reason but never re-collect, so they answer in one model call (~90-245s) instead of re-running the whole pipeline. The question itself is the retrieval query, so the model sees the passages that bear on what was asked. Pass `?incident=<row>` to ask about an earlier incident; the default is the most recent.

`/chat` returns the answer as prose alongside a `retrieval` block naming the mode, chunk counts, and which sources fed the answer — so an answer can always be traced back to the evidence it came from.
