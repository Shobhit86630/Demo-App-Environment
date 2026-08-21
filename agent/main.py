from fastapi import FastAPI
from tools.docker import get_container_logs
from tools.database import inspect_database
from tools.git import get_git_history
from tools.system import get_network_connections
from tools.systemd import get_systemd_logs
from evidence_collector import collect_incident_evidence

app = FastAPI(
    title="Sentinel",
    description="AI-powered incident investigation platform",
    version="0.1.0",
)


@app.get("/")
def root():
    return {
        "name": "Sentinel",
        "version": "0.1.0",
        "status": "running",
    }

@app.get("/incidents/investigate")
def investigate_incident():
    return {
        "incident_id": "INC-001",
        "status": "investigating",
        "evidence": collect_incident_evidence(),
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