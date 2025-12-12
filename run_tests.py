#!/usr/bin/env python3
"""
Helper to run pytest in a local venv.

Behavior:
- Creates .venv if missing.
- Installs dev requirements from requirements-dev.txt into .venv.
- Runs pytest using the venv's Python.

This is optional; runtime usage of the player does not require the venv.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent
VENV_DIR = REPO_ROOT / ".venv"
REQS = REPO_ROOT / "requirements-dev.txt"


def venv_python() -> Path:
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def ensure_venv() -> None:
    if VENV_DIR.exists():
        return
    print(f"[info] creating venv at {VENV_DIR}")
    subprocess.check_call([sys.executable, "-m", "venv", str(VENV_DIR)])


def install_deps(py: Path) -> None:
    if not REQS.exists():
        print(f"[warn] {REQS} not found; skipping install")
        return
    print(f"[info] installing dev requirements from {REQS}")
    subprocess.check_call([str(py), "-m", "pip", "install", "-r", str(REQS)])


def run_pytest(py: Path) -> int:
    cmd = [str(py), "-m", "pytest"]
    print(f"[info] running pytest via: {' '.join(cmd)}")
    return subprocess.call(cmd)


def main() -> int:
    ensure_venv()
    py = venv_python()
    install_deps(py)
    return run_pytest(py)


if __name__ == "__main__":
    raise SystemExit(main())
