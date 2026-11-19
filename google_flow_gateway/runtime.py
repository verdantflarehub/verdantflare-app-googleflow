"""One lifecycle owns both listeners and dispatch. Desktop hooks are supplied by the image."""
import asyncio
from contextlib import contextmanager
import json
import os
import time
from pathlib import Path
import secrets
import signal
import socket

import httpx
import uvicorn

from . import __version__
from .dashboard import DashboardAssets
from .discovery import Config, DiscoveryError, Publisher
from .discovery_store import Store
from .gflow_client import GFlow, local_session
from .gpu_lease import Lease
from .management import management
from .media import Media
from .policy import Error
from .queue import Queue
from .scheduler import Scheduler
from .server import Gateway, execution
from .turn import credentials


class Listener(uvicorn.Server):
    @contextmanager
    def capture_signals(self):
        yield


async def command(program, *args):
    proc = await asyncio.create_subprocess_exec(program, *args, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.DEVNULL)
    stdout, _ = await proc.communicate()
    if proc.returncode:
        raise Error("desktop_unavailable", 503)
    return stdout.decode().strip()


async def run():
    env = os.environ
    os.umask(0o077)
    execution_token, management_token = env["GOOGLE_FLOW_INTERNAL_TOKEN"], env["GOOGLE_FLOW_MANAGEMENT_TOKEN"]
    if min(len(execution_token), len(management_token)) < 32 or execution_token == management_token:
        raise ValueError("invalid_listener_tokens")
    state = Path(env.get("GOOGLE_FLOW_STATE_DIR", "/data/google-flow/state"))
    output = Path(env.get("GFLOW_CLI_OUTPUT_DIR", "/data/google-flow/output"))
    state.mkdir(parents=True, exist_ok=True)
    assets = DashboardAssets(env["GOOGLE_FLOW_DASHBOARD_DIR"])
    org, station = env["GOOGLE_FLOW_ORGANIZATION_ID"], env["GOOGLE_FLOW_STATION_ID"]
    hook = env["GOOGLE_FLOW_DESKTOP_CONTROL"]
    gpu = await command("nvidia-smi", "--query-gpu=uuid", "--format=csv,noheader")
    if not gpu or len(gpu.splitlines()) != 1:
        raise ValueError("gpu_identity_unavailable")
    identity = dict(pod_uid=env["GOOGLE_FLOW_POD_UID"], container_id=env["GOOGLE_FLOW_CONTAINER_ID"],
                    boot_id=Path("/proc/sys/kernel/random/boot_id").read_text().strip())
    scheduler = Scheduler(env["GOOGLE_FLOW_SCHEDULER_URL"], env["GOOGLE_FLOW_SCHEDULER_TOKEN"], gpu,
                          int(env["GOOGLE_FLOW_PEAK_BUDGET_MIB"]))
    token = secrets.token_urlsafe(48)
    client = GFlow(local_session(token), json.loads(env.get("GOOGLE_FLOW_MODEL_CREDITS", "{}")))
    daemon = None

    async def settle():
        nonlocal daemon
        if daemon and daemon.returncode is None:
            daemon.terminate()
            await daemon.wait()
        daemon = None
        await command(hook, "stop-and-wait-for-zero-gpu-processes")

    async def desktop(action):
        if action == "turn":
            return credentials(env["GOOGLE_FLOW_TURN_URLS"].split(","), env["GOOGLE_FLOW_TURN_SECRET"])
        if action not in ("status", "open", "close", "login"):
            raise Error("invalid_field", field="action")
        return json.loads(await command(hook, action))

    # Bind both sockets before touching the durable queue or acquiring a lease.
    sockets, servers, tasks = [], [], []
    stop = asyncio.Event()
    q = lease = publisher = None
    try:
        for port in (8000, 8001):
            sock = socket.socket()
            sockets.append(sock)
            sock.bind(("0.0.0.0", port))
            sock.setblocking(False)
        q = Queue(state / "queue.sqlite3")
        lease = Lease(q, scheduler, identity, gpu, settle,
                      int(env.get("GOOGLE_FLOW_LEASE_RECONCILE_TIMEOUT_MINUTES", "30")))
        g = Gateway(q, client, Media(state / "cache"), output, org)
        g.admitted = lambda: lease.valid and daemon is not None and daemon.returncode is None
        apps = [execution(g, execution_token), management(g, management_token, station, assets, lease, desktop)]
        for app, sock in zip(apps, sockets):
            server = Listener(uvicorn.Config(app, access_log=False, log_level="warning"))
            servers.append(server)
            tasks.append(asyncio.create_task(server.serve(sockets=[sock])))
        if env.get("CHANNEL_DISCOVERY_ETCD_URL"):
            config = Config(env["CHANNEL_DISCOVERY_ETCD_URL"], env["CHANNEL_DISCOVERY_SCOPE_ID"],
                            env["ETCD_USERNAME"], env["ETCD_PASSWORD"], execution_url=env["CHANNEL_EXECUTION_URL"])
            publisher = Publisher(config, Store(state / "discovery.sqlite3"), __version__, [
                dict(capability_id=k, adapter_version="1", execution_state="adapter_connected_unverified")
                for k in ("image", "video")])

        async def lifecycle():
            nonlocal daemon
            while not stop.is_set():
                if not lease.valid:
                    if await lease.recover():
                        await command(hook, "start")
                        allowed = {"PATH", "HOME", "LANG", "LC_ALL", "DISPLAY", "WAYLAND_DISPLAY",
                                   "DBUS_SESSION_BUS_ADDRESS", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY"}
                        process_env = {key: value for key, value in env.items()
                                       if key.upper() in allowed or key.startswith(("XDG_", "GFLOW_CLI_"))}
                        process_env.update(GFLOW_CLI_DAEMON_TOKEN=token, GFLOW_CLI_TELEMETRY="0",
                                           GFLOW_CLI_UPDATE_CHECK="false", GFLOW_CLI_INCIDENT_CAPTURE="false")
                        daemon = await asyncio.create_subprocess_exec("gflow", "serve", "--host", "127.0.0.1",
                             "--port", "18000", env=process_env, stdout=asyncio.subprocess.DEVNULL,
                             stderr=asyncio.subprocess.DEVNULL)
                else:
                    await lease.heartbeat()
                await asyncio.sleep(30)

        async def work():
            next_cleanup = 0
            retention = int(env.get("GOOGLE_FLOW_CACHE_RETENTION_HOURS", "72"))
            if retention <= 0:
                raise ValueError("invalid_cache_retention")
            while True:
                if lease.valid:
                    await g.tick()
                if time.monotonic() >= next_cleanup:
                    cutoff = time.time() - retention * 3600
                    protected = {r[0] for r in q.db.execute("SELECT digest FROM inputs")}
                    protected.update(json.loads(r[0])["sha256"] for r in q.db.execute(
                        """SELECT metadata FROM results JOIN calls ON calls.id=results.id
                           WHERE started>=? OR state IN ('running','dispatching')""", (cutoff,)))
                    g.media.cleanup(cutoff, protected)
                    next_cleanup = time.monotonic() + 3600
                await asyncio.sleep(1)

        async def publish():
            while True:
                active = lease.valid and not q.setting("pause")
                try:
                    await asyncio.to_thread(publisher.set_state, "enabled" if active else "disabled",
                                            "ready" if active else "paused")
                    await asyncio.to_thread(publisher.publish)
                except DiscoveryError:
                    if publisher.fenced:
                        raise
                await asyncio.sleep(60)

        tasks.extend([asyncio.create_task(lifecycle()), asyncio.create_task(work())])
        if publisher:
            tasks.append(asyncio.create_task(publish()))
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: stop.set())
        tasks.append(asyncio.create_task(stop.wait()))
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        stop.set()
        for server in servers:
            server.should_exit = True
        for task in tasks[len(servers):]:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            if lease and lease.valid:
                await lease.close()
        finally:
            if publisher:
                await asyncio.to_thread(publisher.close)
            for sock in sockets:
                sock.close()
            if q:
                q.db.close()
            await scheduler.http.aclose()


def main():
    try:
        asyncio.run(run())
    except (OSError, RuntimeError, ValueError, KeyError, Error, httpx.HTTPError):
        raise SystemExit("gateway_startup_or_listener_failed") from None


if __name__ == "__main__":
    main()
