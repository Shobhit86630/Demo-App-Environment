import subprocess


def get_systemd_logs(service: str, lines: int = 100, since: str = None) -> dict:
    """
    Retrieve logs for an approved systemd service.

    With `since` (anything journalctl's --since accepts, e.g. "2024-01-01
    00:00:00"), only entries after that point are returned - the live
    collector's delta fetch.
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

    command = ["journalctl", "-u", service, "-n", str(lines), "--no-pager"]

    if since:
        command += ["--since", since]

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
            "service": service,
            "logs": "",
            "error": f"Could not run journalctl: {error}",
        }

    return {
        "success": result.returncode == 0,
        "service": service,
        "logs": result.stdout,
        "error": result.stderr if result.returncode != 0 else None,
    }