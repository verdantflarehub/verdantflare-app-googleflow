#!/opt/google-flow/bin/python
"""Manage the lease-owned desktop and its optional relay session."""
import fcntl
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time

STACK = ("dbus-system", "dbus-session", "pipewire", "wireplumber",
         "pipewire-pulse", "smithay-display", "kwin-wayland")
CTL = ("/usr/bin/supervisorctl", "-c", "/app/runtime/supervisord.conf")


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


def gpu_idle():
    # Root inspects all PIDs in the container PID namespace, including graphics clients.
    devices = {p.stat().st_rdev for pattern in ("/dev/nvidia[0-9]*", "/dev/dri/renderD*", "/dev/dri/card*")
               for p in Path("/").glob(pattern[1:]) if p.is_char_device()}
    if not devices:
        raise RuntimeError("GPU devices unavailable")
    for proc in Path("/proc").glob("[0-9]*"):
        try:
            for fd in (proc / "fd").iterdir():
                try:
                    info = fd.stat()
                except FileNotFoundError:
                    continue
                if stat.S_ISCHR(info.st_mode) and info.st_rdev in devices:
                    return False
        except (FileNotFoundError, ProcessLookupError):
            continue
    return True


def handle(action):
    from google_flow_gateway.turn import credentials

    rtc = Path(os.environ["GOOGLE_FLOW_RTC_FILE"])
    if action == "start":
        for program in STACK:
            change(program, True)
        socket = Path(os.environ["XDG_RUNTIME_DIR"]) / os.environ["WAYLAND_DISPLAY"]
        deadline = time.monotonic() + 10
        while not socket.is_socket():
            if time.monotonic() >= deadline:
                raise RuntimeError("Wayland display unavailable")
            time.sleep(.2)
    elif action in ("close", "stop-and-wait-for-zero-gpu-processes"):
        change("flow-login", False)
        change("start-webrtc", False)
        rtc.unlink(missing_ok=True)
        if action != "close":
            for program in reversed(STACK):
                change(program, False)
            for sig in ("TERM", "KILL"):
                subprocess.run(["pkill", "-" + sig, "-u", str(os.getuid()), "-x", "chrome"],
                               check=False)
                for _ in range(10):
                    check = subprocess.run(["sudo", "-n", "/usr/bin/python3", "-I", __file__, "gpu-idle"])
                    if check.returncode == 0:
                        return {"open": False}
                    time.sleep(.2)
            raise RuntimeError("GPU settlement unconfirmed")
    elif action == "open":
        if not running("kwin-wayland"):
            raise RuntimeError("desktop unavailable")
        config = credentials(os.environ["GOOGLE_FLOW_TURN_URLS"].split(","), os.environ["GOOGLE_FLOW_TURN_SECRET"])
        change("start-webrtc", False)
        temp = rtc.with_suffix(".tmp")
        temp.write_text(json.dumps(config), encoding="utf-8")
        temp.replace(rtc)
        change("start-webrtc", True)
    elif action == "login":
        if not running("start-webrtc"):
            raise RuntimeError("open desktop first")
        change("flow-login", True)
    elif action != "status":
        raise ValueError("unknown desktop action")
    return {"available": running("kwin-wayland"), "open": running("start-webrtc"),
            "viewer_path": os.environ.get("GOOGLE_FLOW_VIEWER_PATH", "")}


if __name__ == "__main__":
    action, = sys.argv[1:]
    if action == "gpu-idle":
        sys.exit(0 if gpu_idle() else 1)
    os.umask(0o077)
    with (Path(os.environ["XDG_RUNTIME_DIR"]) / "google-flow-control.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        print(json.dumps(handle(action)))
