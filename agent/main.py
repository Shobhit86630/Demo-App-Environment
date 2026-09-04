import asyncio
import os

from fastapi import FastAPI
from tools.docker import get_container_logs
from tools.database import inspect_database
from tools.git import get_git_history
from tools.system import get_network_connections
from tools.systemd import get_systemd_logs
from evidence_collector import collect_incident_evidence
from live_collector import LiveCollector
from reasoning import analyze_incident, answer_question, check_llm
from store import store_evidence, store_stats
from retrieval import select_context, format_context

app = FastAPI(
    title="Sentinel",
    description="AI-powered incident investigation platform",
    version="0.1.0",
)

LIVE_COLLECTOR = LiveCollector()

# Opt-out: set SENTINEL_LIVE_COLLECTION=false to run purely on manual
# /incidents/investigate calls, e.g. when the lab stack is down.
LIVE_COLLECTION_ENABLED = os.environ.get("SENTINEL_LIVE_COLLECTION", "true").lower() != "false"

# Held so the task isn't garbage-collected once start_live_collection() returns
# - asyncio only keeps a weak reference to a task once nothing awaits it.
_live_collection_task = None


@app.on_event("startup")
async def start_live_collection():
    global _live_collection_task

    if LIVE_COLLECTION_ENABLED:
        _live_collection_task = asyncio.create_task(LIVE_COLLECTOR.run_forever())


@app.get("/")
def root():
    return {
        "name": "Sentinel",
        "version": "0.1.0",
        "status": "running",
    }

@app.get("/incidents/investigate")
def investigate_incident(analyze: bool = True, query: str = ""):
    """sources -> evidence -> store -> retrieval -> LLM."""
    evidence = collect_incident_evidence()

    # Persist at full fidelity before anything is dropped, so the reasoning
    # stage narrows a stored record rather than a one-shot in-memory snapshot.
    stored = store_evidence(evidence)

    retrieval = (
        select_context(stored["incident_row"], query=query)
        if stored["success"]
        else {"success": False, "error": stored["error"], "chunks": []}
    )

    # The reasoning pass is opt-out: local inference takes tens of seconds, so
    # ?analyze=false gives back the evidence and retrieval immediately.
    analysis = None

    if analyze:
        context = (
            format_context(retrieval, incident_row=stored["incident_row"])
            if retrieval["success"]
            else None
        )
        analysis = analyze_incident(evidence, context=context)

    return {
        "incident_id": "INC-001",
        "status": "investigating",
        "evidence": evidence,
        "storage": stored,
        "retrieval": {key: value for key, value in retrieval.items() if key != "chunks"},
        "analysis": analysis,
    }

@app.get("/tools/docker/logs")
def docker_logs(
    container: str = "api",
    lines: int = 100,
):
    return get_container_logs(container, lines)


@app.get("/tools/database")
def database():
    return inspect_database()


@app.get("/tools/git/history")
def git_history(limit: int = 10):
    return get_git_history(limit)


@app.get("/tools/network")
def network():
    return get_network_connections()


@app.get("/tools/systemd/logs")
def systemd_logs(
    service: str = "docker",
    lines: int = 100,
):


    return get_systemd_logs(service, lines)


@app.get("/tools/llm/health")
def llm_health(model: str = None):
    return check_llm(model)


@app.get("/tools/store/stats")
def store_statistics():
    return store_stats()


@app.get("/incidents/live")
def live_status():
    """Status of the background collector feeding the store in real time."""
    return {
        "enabled": LIVE_COLLECTION_ENABLED,
        **LIVE_COLLECTOR.stats(),
    }


@app.get("/chat")
def chat(q: str, incident: int = 0, budget: int = 6000):
    """Ask a question about stored evidence — retrieval-augmented, no re-collection.

    Defaults to whichever incident most recently received evidence (the live
    incident, if live collection is running and nothing newer has been
    manually investigated) rather than whichever incident row has the highest
    id — see `store_stats()`. The question itself is the retrieval query, so
    the model sees the passages that actually bear on what was asked.
    """
    if not incident:
        stats = store_stats()

        if not stats["success"] or not stats.get("latest_incident_row"):
            return {
                "success": False,
                "question": q,
                "answer": None,
                "error": "No stored incidents yet. Run /incidents/investigate first.",
            }

        incident = stats["latest_incident_row"]

    retrieval = select_context(incident, query=q, budget_chars=budget)

    if not retrieval["success"]:
        return {
            "success": False,
            "question": q,
            "incident_row": incident,
            "answer": None,
            "error": retrieval["error"],
        }

    result = answer_question(q, format_context(retrieval, incident_row=incident))

    return {
        "success": result["success"],
        "question": q,
        "incident_row": incident,
        "model": result["model"],
        "retrieval": {
            "mode": retrieval["mode"],
            "selected": retrieval["selected"],
            "available": retrieval["available"],
            "used_chars": retrieval["used_chars"],
            "sources": retrieval["sources"],
        },
        "answer": result["answer"],
        "error": result["error"],
    }
