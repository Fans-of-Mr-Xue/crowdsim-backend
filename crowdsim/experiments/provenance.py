"""Best-effort reproducibility metadata for each run."""

from __future__ import annotations

from pathlib import Path
import platform
import subprocess
import sys


def _git_state(path: Path) -> dict:
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=path, capture_output=True, text=True, timeout=3, check=True).stdout.strip()
        status = subprocess.run(["git", "status", "--porcelain"], cwd=path, capture_output=True, text=True, timeout=3, check=True).stdout
        return {"path": str(path), "commit": commit, "dirty": bool(status.strip())}
    except Exception as exc:
        return {"path": str(path), "commit": None, "dirty": None, "error": f"{type(exc).__name__}: {exc}"}


def runtime_provenance() -> dict:
    repository_root = Path(__file__).resolve().parents[2]
    return {
        "python": sys.version,
        "platform": platform.platform(),
        "repositories": {
            "crowdsim_backend": _git_state(repository_root),
        },
        "contractVersion": "1.0",
    }
