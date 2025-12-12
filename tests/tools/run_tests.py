#!/usr/bin/env python3
"""
Helper to run pytest in a local venv.

Behavior:
- Creates .venv if missing.
- Installs dev requirements from requirements-dev.txt into .venv.
- Runs pytest using the venv's Python.
- Optional: with --library /path/to/music, runs integration tests (album-spread) against that library.

This is optional; runtime usage of the player does not require the venv.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


# repo root (tests/tools/run_tests.py -> tools -> tests -> repo)
REPO_ROOT = Path(__file__).resolve().parents[2]
VENV_DIR = REPO_ROOT / ".venv"
REQS = REPO_ROOT / "tests" / "requirements-dev.txt"


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
    cmd = [str(py), "-m", "pytest", "tests/unit"]
    print(f"[info] running pytest via: {' '.join(cmd)}")
    return subprocess.call(cmd)


def run_integration(py: Path, library: Path) -> int:
    if not library.is_dir():
        print(f"[error] library path not found: {library}")
        return 1
    env = os.environ.copy()
    env["MPV_MUSIC_LIBRARY"] = str(library)
    cmd = [str(py), "-m", "pytest", "-s", "tests/integration"]
    print(f"[info] running integration tests via: {' '.join(cmd)} (MPV_MUSIC_LIBRARY={library})")
    return subprocess.call(cmd, env=env)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run tests for mpv-music-wrapper")
    parser.add_argument("--library", type=Path, help="Optional library path to validate album-spread recent-album avoidance")
    return parser.parse_args(argv)


def main() -> int:
    args = parse_args(sys.argv[1:])
    ensure_venv()
    py = venv_python()
    install_deps(py)
    rc = run_pytest(py)
    if args.library:
        rc_integration = run_integration(py, args.library)
        rc = rc or rc_integration
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
