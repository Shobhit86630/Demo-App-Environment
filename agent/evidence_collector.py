from evidence import Evidence
from tools.docker import get_container_logs
from tools.database import inspect_database
from tools.git import get_git_history
from tools.system import get_network_connections


def collect_incident_evidence():
    evidence = []

    # 1. Application logs
    logs = get_container_logs(
        "sentinel-api",
        100
    )

    evidence.append(
        Evidence(
            source="docker_logs",
            category="application",
            finding="Retrieved API container logs",
            raw_data=logs.get("logs", ""),
            severity="ERROR" if not logs["success"] else "INFO",
        )
    )

    # 2. Database
    database = inspect_database()

    evidence.append(
        Evidence(
            source="postgresql",
            category="database",
            finding=(
                "Database inspection successful"
                if database["success"]
                else "Database inspection failed"
            ),
            raw_data=str(database),
            severity="INFO" if database["success"] else "ERROR",
        )
    )

    # 3. Git history
    git = get_git_history(10)

    evidence.append(
        Evidence(
            source="git",
            category="version_control",
            finding="Retrieved recent Git history",
            raw_data=git.get("commits", ""),
            severity="INFO",
        )
    )

    # 4. Network
    network = get_network_connections()

    evidence.append(
        Evidence(
            source="network",
            category="network",
            finding="Retrieved network connections",
            raw_data=network.get("output", ""),
            severity="INFO" if network["success"] else "ERROR",
        )
    )

    return [item.to_dict() for item in evidence]