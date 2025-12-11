#!/usr/bin/env python3
"""
mpv-send-key.py — send a control command to mpv IPC sockets.

Usage:
  mpv-send-key.py pause
  mpv-send-key.py next
  mpv-send-key.py prev

Optional:
  MPV_SEND_DEBUG=1 mpv-send-key.py pause
    -> print which sockets are found and any errors.

  mpv-send-key.py pause '/tmp/mpv-main'
    -> only target a specific socket (or glob pattern).
"""

import glob
import json
import os
import socket
import stat
import sys
from typing import List


DEBUG = os.environ.get("MPV_SEND_DEBUG") == "1"


def debug(msg: str) -> None:
    if DEBUG:
        print(f"[mpv-send-key] {msg}", file=sys.stderr)


def usage() -> None:
    print(f"Usage: {sys.argv[0]} {{pause|next|prev}} [SOCKET_GLOB]", file=sys.stderr)
    sys.exit(1)


def action_to_command(action: str) -> dict:
    a = action.lower()
    if a in ("pause", "play", "playpause", "toggle"):
        return {"command": ["cycle", "pause"]}
    elif a in ("next", "n", "forward"):
        return {"command": ["playlist-next", "weak"]}
    elif a in ("prev", "previous", "p", "back"):
        return {"command": ["playlist-prev", "weak"]}
    else:
        usage()
        raise SystemExit


def is_socket(path: str) -> bool:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False
    return stat.S_ISSOCK(st.st_mode)


def send_command_to_socket(path: str, payload: str) -> None:
    debug(f"Sending to socket: {path}")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(0.3)
            s.connect(path)
            s.sendall(payload.encode("utf-8") + b"\n")

            # Politely half-close write side
            try:
                s.shutdown(socket.SHUT_WR)
            except OSError as e:
                debug(f"shutdown() failed on {path}: {e!r}")

            # Drain any reply/events until mpv closes or we time out
            while True:
                try:
                    chunk = s.recv(4096)
                except socket.timeout:
                    debug(f"recv() timeout on {path}")
                    break
                if not chunk:
                    debug(f"EOF from {path}")
                    break

    except ConnectionRefusedError as e:
        # Socket file exists but nothing is listening -> stale.
        debug(f"Connection refused on {path}, treating as stale: {e!r}")
        try:
            os.unlink(path)
            debug(f"Deleted stale socket {path}")
        except FileNotFoundError:
            debug(f"Stale socket {path} vanished before unlink")
        except OSError as e2:
            debug(f"Failed to delete stale socket {path}: {e2!r}")

    except OSError as e:
        # Other errors: permission, weird socket, etc.
        debug(f"OSError talking to {path}: {e!r}")


def main() -> None:
    if len(sys.argv) < 2:
        usage()

    cmd = action_to_command(sys.argv[1])
    payload = json.dumps(cmd, separators=(",", ":"))

    if len(sys.argv) >= 3:
        pattern = sys.argv[2]
    else:
        pattern = "/tmp/mpv-*"

    debug(f"Using socket glob: {pattern}")

    candidates: List[str] = glob.glob(pattern)
    debug(f"Glob matches: {candidates!r}")

    sockets = [p for p in candidates if is_socket(p)]
    debug(f"Filtered sockets (type=SOCK): {sockets!r}")

    if not sockets:
        debug("No sockets found, nothing to do.")
        return

    for path in sockets:
        send_command_to_socket(path, payload)


if __name__ == "__main__":
    main()
