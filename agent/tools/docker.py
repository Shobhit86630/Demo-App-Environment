import subprocess


def get_container_logs(container: str, lines: int = 100, since: str = None) -> dict:
    """
    Retrieve logs from an approved Docker container.

    With `since` (an RFC3339 timestamp or docker's relative duration syntax,
    e.g. "2024-01-01T00:00:00" or "30s"), only lines emitted after that point
    are returned - what the live collector uses to fetch just the delta since
    its last poll instead of re-reading (and re-embedding) the same tail.
    """

    allowed_containers = {
        "sentinel-api",
        "sentinel-postgres",
        "sentinel-redis",
    }

    if container not in allowed_containers:
        return {
            "success": False,
            "error": f"Container '{container}' is not allowed."
        }

    if lines < 1 or lines > 1000:
        return {
            "success": False,
            "error": "lines must be between 1 and 1000."
        }

    command = ["docker", "logs", "--tail", str(lines)]

    if since:
        command += ["--since", since]

    command.append(container)

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.SubprocessError, OSError) as error:
        return {
            "success": False,
            "container": container,
            "logs": "",
            "error": f"Could not run docker logs: {error}",
        }

    return {
        "success": result.returncode == 0,
        "container": container,
        "logs": result.stdout + result.stderr,
        "error": result.stderr if result.returncode != 0 else None,
    }