#!/usr/bin/env python3
"""Keep Windows awake while a Claude Code or Codex turn is active.

Each turn gets its own holder process so concurrent agents can finish
independently. A four-hour lease releases the request after a crash.
Only idle system sleep is blocked; display timeout and explicit sleep remain.
Set AI_COLLAB_NO_AWAKE=1 to disable. On non-Windows systems this is a no-op.
"""

import argparse
import ctypes
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path


STATE_DIR = Path.home() / ".ai-collab" / "awake"
MAX_SECONDS = 4 * 60 * 60
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008


def safe_key(value):
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(value))[:64] or "nosession"


def lease_path(agent, session_id):
    return STATE_DIR / f"{safe_key(agent)}-{safe_key(session_id)}.json"


def report_error(message):
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with (STATE_DIR / "errors.log").open("a", encoding="utf-8") as stream:
            stream.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except OSError:
        pass


def enabled():
    """Only Windows supports this; AI_COLLAB_NO_AWAKE=1 turns it off."""
    return sys.platform == "win32" and not os.environ.get("AI_COLLAB_NO_AWAKE")


def start(agent, session_id):
    if not enabled():
        return
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = lease_path(agent, session_id)
    token = uuid.uuid4().hex
    temporary = path.with_name(f"{path.name}.{token}.tmp")
    temporary.write_text(json.dumps({"token": token, "started": time.time()}), encoding="utf-8")
    os.replace(temporary, path)
    try:
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "_hold", str(path), token],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=CREATE_NO_WINDOW | DETACHED_PROCESS,
        )
    except Exception:
        path.unlink(missing_ok=True)
        raise


def stop(agent, session_id):
    lease_path(agent, session_id).unlink(missing_ok=True)


def holder(path, token):
    if sys.platform != "win32":
        return 0
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    execution_state = kernel32.SetThreadExecutionState
    execution_state.argtypes = [ctypes.c_uint32]
    execution_state.restype = ctypes.c_uint32
    if not execution_state(ES_CONTINUOUS | ES_SYSTEM_REQUIRED):
        report_error(f"SetThreadExecutionState failed: {ctypes.get_last_error()}")
        return 1

    ready = path.with_name(f"{path.stem}.{token}.ready")
    try:
        ready.write_text(str(os.getpid()), encoding="ascii")
        deadline = time.monotonic() + MAX_SECONDS
        while time.monotonic() < deadline:
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                break
            if current.get("token") != token:
                break
            time.sleep(1)
        else:
            report_error(f"Lease expired after {MAX_SECONDS} seconds: {path.name}")
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
                if current.get("token") == token:
                    path.unlink(missing_ok=True)
            except (OSError, ValueError):
                pass
    finally:
        ready.unlink(missing_ok=True)
        execution_state(ES_CONTINUOUS)
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent", default="agent")
    parser.add_argument("--event", choices=("start", "stop", "status"), required=True)
    args = parser.parse_args()

    if args.event == "status":
        for path in sorted(STATE_DIR.glob("*.json")):
            ready = list(STATE_DIR.glob(f"{path.stem}.*.ready"))
            print(f"{path.stem}: {'holding' if ready else 'starting'}")
        return 0

    try:
        raw = sys.stdin.buffer.read().decode("utf-8", "replace")
        data = json.loads(raw) if raw.strip() else {}
        session_id = data.get("session_id") or data.get("thread_id") or "nosession"
        if args.event == "start":
            start(args.agent, session_id)
        else:
            stop(args.agent, session_id)
    except Exception as error:
        report_error(f"{args.agent}/{args.event}: {error}")
    sys.stdout.write("{}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "_hold":
        sys.exit(holder(Path(sys.argv[2]), sys.argv[3]))
    sys.exit(main())
