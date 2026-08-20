import subprocess


REPOSITORY = "/home/shobhit-v15/Application-Demo-Environment"


def get_git_history(limit: int = 10) -> dict:
    if limit < 1 or limit > 50:
        return {
            "success": False,
            "error": "limit must be between 1 and 50."
        }

    result = subprocess.run(
        [
            "git",
            "-C",
            REPOSITORY,
            "log",
            "--oneline",
            f"-{limit}",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )

    return {
        "success": result.returncode == 0,
        "commits": result.stdout,
        "error": result.stderr if result.returncode != 0 else None,
    }