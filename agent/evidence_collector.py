from evidence import Evidence
from tools.docker import get_container_logs
from tools.database import inspect_database
from tools.git import get_git_history
from tools.system import get_network_connections
from tools.systemd import get_systemd_logs


def build_evidence(source, category, subject, result, payload_key):
    """Wrap one tool result as Evidence, composing the finding text per severity.

    `subject` is a noun phrase ("database state", "API container logs"), not a
    full sentence - it gets reused as the subject of three different sentences
    below, and a past-tense clause reused that way reads as "Retrieved X
    failed", which asserts the retrieval both happened and didn't.

    Severity carries three states, not two, because a source that succeeds and
    returns nothing is not healthy - `docker logs` on a container that never
    started exits 0 with empty output, and reporting that as INFO hides the most
    important source from the reasoning stage.
    """
    payload = result.get(payload_key)

    # A non-string payload (a count, a number) carries no context on its own, so
    # serialize the whole result rather than stringifying the bare value.
    raw_data = payload if isinstance(payload, str) else str(result)

    if not result.get("success"):
        return Evidence(
            source=source,
            category=category,
            finding=f"{subject} unavailable: {result.get('error', 'unknown error')}",
            raw_data=raw_data,
            severity="ERROR",
        )

    if not raw_data.strip():
        return Evidence(
            source=source,
            category=category,
            finding=f"{subject}: no data — source reachable but empty",
            raw_data="",
            severity="WARNING",
        )

    return Evidence(
        source=source,
        category=category,
        finding=f"Retrieved {subject}",
        raw_data=raw_data,
        severity="INFO",
    )


def collect_incident_evidence():
    evidence = []

    # 1. Application logs
    logs = get_container_logs("sentinel-api", 100)
    evidence.append(
        build_evidence("docker_logs", "application", "API container logs", logs, "logs")
    )

    # 2. Database
    database = inspect_database()
    evidence.append(
        build_evidence("postgresql", "database", "database state", database, "connections")
    )

    # 3. Git history
    git = get_git_history(10)
    evidence.append(
        build_evidence("git", "version_control", "recent Git history", git, "commits")
    )

    # 4. Network
    network = get_network_connections()
    evidence.append(
        build_evidence("network", "network", "network connections", network, "output")
    )

    # 5. Docker daemon journal
    systemd = get_systemd_logs("docker", 100)
    evidence.append(
        build_evidence("systemd", "host", "docker daemon journal", systemd, "logs")
    )

    return [item.to_dict() for item in evidence]
