import subprocess


def get_network_connections() -> dict:
    """
    Retrieve listening TCP/UDP sockets from the host.
    """

    result = subprocess.run(
        ["ss", "-tulpn"],
        capture_output=True,
        text=True,
        timeout=10,
    )

    return {
        "success": result.returncode == 0,
        "output": result.stdout,
        "error": result.stderr if result.returncode != 0 else None,
    }