import subprocess


def get_network_connections() -> dict:
    """
    Retrieve listening TCP/UDP sockets from the host.
    """

    try:
        result = subprocess.run(
            ["ss", "-tulpn"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.SubprocessError, OSError) as error:
        return {"success": False, "output": "", "error": f"Could not run ss: {error}"}

    return {
        "success": result.returncode == 0,
        "output": result.stdout,
        "error": result.stderr if result.returncode != 0 else None,
    }