import subprocess


def get_systemd_logs(service: str, lines: int = 100) -> dict:
    """
    Retrieve logs for an approved systemd service.
    """

    allowed_services = {
        "docker",
    }

    if service not in allowed_services:
        return {
            "success": False,
            "error": f"Service '{service}' is not allowed."
        }

    if lines < 1 or lines > 1000:
        return {
            "success": False,
            "error": "lines must be between 1 and 1000."
        }

    result = subprocess.run(
        [
            "journalctl",
            "-u",
            service,
            "-n",
            str(lines),
            "--no-pager",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )

    return {
        "success": result.returncode == 0,
        "service": service,
        "logs": result.stdout,
        "error": result.stderr if result.returncode != 0 else None,
    }