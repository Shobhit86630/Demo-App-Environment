# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

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

**Host coupling.** The agent runs on the host, not in a container: it reads Docker container logs by name (`sentinel-api`, `sentinel-postgres`, `sentinel-redis` — the `container_name` values in `docker-compose.yml`), connects to Postgres on the *published* port `5433`, and reads `journalctl -u docker` and `ss -tulpn`. Renaming a container in the compose file silently breaks `docker.py`'s allowlist, and `git.py`'s `REPOSITORY` constant is an absolute path to this checkout — it must be updated if the checkout ever moves, since a stale path fails silently (git evidence just comes back `success: False`, easy to miss).

**The lab's faults are intentional.** `docker-compose.yml` sets `DB_PASSWORD: wrong_password` while Postgres is initialized with `sentinel`, so `GET /db` on the lab API fails authentication by design. The lab endpoints also have no error handling — `psycopg2.connect` and `redis.ping()` raise straight through to a 500, which is the point. Do not "fix" these unless asked; they are the incidents the agent exists to find.

`demo_app/lab/api/main.py` carries a deliberate mix of fault *classes*, not just one bug repeated, so retrieval and reasoning have something to distinguish between:

| Endpoint | Fault class | Behavior |
|---|---|---|
| `GET /db` | infra / auth | wrong Postgres password, raises straight through |
| `GET /redis` | infra / connectivity | fine unless Redis is down |
| `GET /orders/{order_id}` | application bug | unhandled `KeyError` for any id outside the two seeded orders (`1001`, `1002`) |
| `GET /cache/counter` | data-shape bug | seeds a string key then `INCR`s it — Redis `WRONGTYPE`, not a connection failure |
| `GET /payments/charge` | flaky dependency | simulates an external payment gateway, times out ~40% of calls |

A background daemon thread (started on the lab API's FastAPI `startup` event) also emits a random INFO/WARNING/ERROR log line every 8–20s from a fixed pool (disk pressure, pool exhaustion, auth failures, upstream 503s, GC pauses, TCP resets, plus a few mundane INFO lines — real noise isn't all alarming) — so there is always fresh, varied noise for the live collector to pick up even when nobody is hitting the broken endpoints by hand.

## Working pipeline

`.claude/agents/` defines three project subagents that fan out from the main session:

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

## Live collection

`/incidents/investigate` is pull-based — evidence only lands in the store when someone calls it. `agent/live_collector.py` adds a push side: a background loop, started on FastAPI `startup` in `main.py` (`asyncio.create_task`, ticking on `asyncio.to_thread` so the blocking subprocess/psycopg2 calls don't stall the event loop), that keeps the store filling on its own so `/chat` and `ask.py` always have something current without a manual investigation first.

Two things keep continuous polling cheap instead of letting the store balloon:

1. **Delta fetch for log sources.** `docker.py` and `systemd.py` grew an optional `since=` (RFC3339 / journalctl's `--since` syntax). Each poll only asks for log lines emitted after the previous poll, so a fast interval doesn't mean re-storing and re-embedding the same tail every cycle.
2. **Change-only snapshots for state sources.** Postgres connection count, `ss` output, and `git log` have no native "since" — `live_collector.py` polls them in full each cycle but keeps a fingerprint of the last-stored payload in memory and only writes when it differs. An idle system produces zero new evidence per poll after the first.

All of it accumulates in one rolling incident (`incident_id="INC-LIVE"`) rather than a fresh incident per tick — `store.py`'s `store_evidence()` grew an optional `incident_row` parameter for exactly this: pass one to append evidence (and embed only its new chunks) to an existing incident instead of `INSERT`ing a new `incidents` row every call. Manual `/incidents/investigate` calls still omit it and get their own incident, unaffected.

Config, both read at import time in `live_collector.py` / `main.py`:

- `SENTINEL_POLL_INTERVAL` — seconds between polls, default `20`.
- `SENTINEL_LIVE_COLLECTION` — set to `false` to disable the background loop entirely (e.g. running the agent with the lab stack down).

`GET /incidents/live` reports whether the loop is enabled, the live incident's row id, poll count, last poll time, and the last store/embed error if any. All per-poll state — the log watermark, the snapshot fingerprints, the incident row — lives on the `LiveCollector` instance (`agent/live_collector.py`), not at module scope, so a restart re-baselines every source and starts a new live incident, and a second instance never inherits another one's history.

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

`/chat` and `ask.py` reuse whatever is already stored — they retrieve and reason but never re-collect, so they answer in one model call (~90-245s) instead of re-running the whole pipeline. The default incident is whichever one *most recently received evidence* (`store_stats()`'s `latest_incident_row`, resolved from the newest row in `evidence`, not the highest `incidents.id`) — with live collection enabled that keeps resolving to the rolling `INC-LIVE` incident even after its row was created, unless a later manual `/incidents/investigate` has since added evidence of its own. The question itself is the retrieval query, so the model sees the passages that bear on what was asked. Pass `?incident=<row>` to ask about a specific earlier incident instead.

`/chat` returns the answer as prose alongside a `retrieval` block naming the mode, chunk counts, and which sources fed the answer — so an answer can always be traced back to the evidence it came from.
