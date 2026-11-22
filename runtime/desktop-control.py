#!/opt/google-flow/bin/python
"""Manage the CPU-only Xvfb and on-demand KasmVNC login desktop."""
import fcntl
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

CTL = ("/usr/bin/supervisorctl", "-c", "/app/runtime/supervisord.conf")
KASMVNC_PORT = int(os.environ.get("GOOGLE_FLOW_DESKTOP_PORT", "48083"))


def running(program):
    result = subprocess.run([*CTL, "status", program], capture_output=True, text=True, timeout=5)
    fields = result.stdout.split()
    if len(fields) < 2 or fields[0] != program:
        raise RuntimeError("supervisor unavailable")
    return fields[1] in ("RUNNING", "STARTING", "BACKOFF", "STOPPING")


def change(program, start):
    if running(program) != start:
        subprocess.run([*CTL, "start" if start else "stop", program], check=True,
                       stdout=subprocess.DEVNULL, timeout=15)


def wait_port(port, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError(f"KasmVNC port {port} unavailable")


def _write_login_env():
    """Write per-account Chrome profile & proxy selection for supervisord flow-login."""
    runtime_dir = Path(os.environ.get("XDG_RUNTIME_DIR", "/run/user/1000"))
    runtime_dir.mkdir(parents=True, exist_ok=True)
    home = os.environ.get("GOOGLE_FLOW_LOGIN_HOME") or os.environ.get("GFLOW_CLI_HOME", "/data/google-flow/gflow")
    proxy = os.environ.get("GOOGLE_FLOW_LOGIN_PROXY") or os.environ.get("GFLOW_CLI_BROWSER_PROXY", "")
    acct_id = os.environ.get("GOOGLE_FLOW_LOGIN_ACCOUNT_ID") or "default"
    (runtime_dir / "google-flow-login.json").write_text(
        json.dumps({"account_id": acct_id, "home": home, "proxy": proxy}),
        encoding="utf-8",
    )


def handle(action):
    if action == "start":
        change("xvfb", True)
        deadline = time.monotonic() + 10
        while not Path("/tmp/.X11-unix/X99").is_socket():
            if time.monotonic() >= deadline:
                raise RuntimeError("Xvfb display :99 unavailable")
            time.sleep(0.2)
    elif action in ("close", "stop-and-wait-for-zero-gpu-processes"):
        change("flow-login", False)
        change("openbox", False)
        change("kasmvnc", False)
        if action != "close":
            for sig in ("TERM", "KILL"):
                subprocess.run(["pkill", "-" + sig, "-u", str(os.getuid()), "-x", "chrome"], check=False)
                time.sleep(0.3)
            return {"open": False}
    elif action == "open":
        _write_login_env()
        change("flow-login", False)
        change("kasmvnc", True)
        wait_port(KASMVNC_PORT, timeout=10.0)
        change("openbox", True)
        change("flow-login", True)
    elif action == "login":
        if not running("kasmvnc"):
            raise RuntimeError("open desktop first")
        _write_login_env()
        change("flow-login", False)
        change("flow-login", True)
    elif action != "status":
        raise ValueError("unknown desktop action")
    return {"available": running("xvfb"), "open": running("kasmvnc"),
            "viewer_path": os.environ.get("GOOGLE_FLOW_VIEWER_PATH", "/apps/google-flow/desktop/")}


if __name__ == "__main__":
    action, = sys.argv[1:]
    os.umask(0o077)
    lock_dir = Path(os.environ.get("XDG_RUNTIME_DIR", "/run/user/1000"))
    lock_dir.mkdir(parents=True, exist_ok=True)
    with (lock_dir / "google-flow-control.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        print(json.dumps(handle(action)))
