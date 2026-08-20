import subprocess


def get_container_logs(container: str, lines: int = 100) -> dict:
    """
    Retrieve the last N lines of logs from an approved Docker container.
    """

    allowed_containers = {
        "api",
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

    result = subprocess.run(
        [
            "docker",
            "logs",
            "--tail",
            str(lines),
            container,
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )

    return {
        "success": result.returncode == 0,
        "container": container,
        "logs": result.stdout + result.stderr,
        "error": result.stderr if result.returncode != 0 else None,
    }