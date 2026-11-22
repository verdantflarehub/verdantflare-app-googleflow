"""One lifecycle owns both listeners, multi-account slots, and dispatch. Desktop hooks are supplied by the image."""
import asyncio
from contextlib import contextmanager
import json
import os
import time
from pathlib import Path
import signal
import socket

import uvicorn

from . import __version__
from .dashboard import DashboardAssets
from .discovery import Config, DiscoveryError, Publisher
from .discovery_store import Store
from .gpu_lease import Lease
from .management import management
from .media import Media
from .network_proxies import NetworkProxies
from .policy import Error
from .queue import DEFAULT_ACCOUNT_ID, Queue
from .scheduler import Scheduler
from .server import Gateway, execution
from .slots import AccountSlotManager
from .turn import credentials


class Listener(uvicorn.Server):
    @contextmanager
    def capture_signals(self):
        yield


async def command(program, *args, extra_env=None):
    run_env = os.environ.copy()
    if extra_env:
        run_env.update(extra_env)
    proc = await asyncio.create_subprocess_exec(program, *args, env=run_env, stdout=asyncio.subprocess.PIPE,
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
    default_home = Path(env.get("GFLOW_CLI_HOME", "/data/google-flow/gflow"))
    default_output = Path(env.get("GFLOW_CLI_OUTPUT_DIR", "/data/google-flow/output"))
    accounts_root = Path(env.get("GOOGLE_FLOW_ACCOUNTS_DIR", str(state.parent / "accounts")))
    state.mkdir(parents=True, exist_ok=True)
    accounts_root.mkdir(parents=True, exist_ok=True)
    assets = DashboardAssets(env["GOOGLE_FLOW_DASHBOARD_DIR"])
    org, station = env["GOOGLE_FLOW_ORGANIZATION_ID"], env["GOOGLE_FLOW_STATION_ID"]
    hook = env["GOOGLE_FLOW_DESKTOP_CONTROL"]
    no_gpu = env.get("GOOGLE_FLOW_NO_GPU", "").lower() == "true" or not env.get("GOOGLE_FLOW_SCHEDULER_URL")
    gpu = identity = scheduler = None
    if not no_gpu:
        gpu = await command("nvidia-smi", "--query-gpu=uuid", "--format=csv,noheader")
        if not gpu or len(gpu.splitlines()) != 1:
            raise ValueError("gpu_identity_unavailable")
        identity = dict(pod_uid=env["GOOGLE_FLOW_POD_UID"], container_id=env["GOOGLE_FLOW_CONTAINER_ID"],
                        boot_id=Path("/proc/sys/kernel/random/boot_id").read_text().strip())
        scheduler = Scheduler(env["GOOGLE_FLOW_SCHEDULER_URL"], env["GOOGLE_FLOW_SCHEDULER_TOKEN"], gpu,
                              int(env["GOOGLE_FLOW_PEAK_BUDGET_MIB"]))
    prices = json.loads(env.get("GOOGLE_FLOW_MODEL_CREDITS", "{}"))

    # Bind both sockets before touching the durable queue or starting the display daemon.
    sockets, servers, tasks = [], [], []
    stop = asyncio.Event()
    q = lease = publisher = slot_mgr = None
    desktop_account_id = DEFAULT_ACCOUNT_ID
    try:
        for port in (8000, 8001):
            sock = socket.socket()
            sockets.append(sock)
            sock.bind(("0.0.0.0", port))
            sock.setblocking(False)
        q = Queue(state / "queue.sqlite3")
        slot_mgr = AccountSlotManager(q, env, prices, default_home, default_output, accounts_root)

        async def settle():
            await slot_mgr.stop_all()
            await command(hook, "stop-and-wait-for-zero-gpu-processes")

        async def desktop(action, account_id=None):
            nonlocal desktop_account_id
            if action == "turn":
                if no_gpu:
                    raise Error("desktop_turn_disabled", 404)
                rtc_path = env.get("GOOGLE_FLOW_RTC_FILE")
                if rtc_path and Path(rtc_path).is_file():
                    try:
                        expires_at = json.loads(Path(rtc_path).read_text(encoding="utf-8")).get("expires_at", 0)
                    except (OSError, ValueError):
                        expires_at = 0
                    if expires_at - time.time() < 120:
                        await command(hook, "open")
                return credentials(env["GOOGLE_FLOW_TURN_URLS"].split(","), env["GOOGLE_FLOW_TURN_SECRET"])
            if action not in ("status", "open", "close", "login"):
                raise Error("invalid_field", field="action")
            extra_env = None
            if action in ("open", "login"):
                aid = account_id or DEFAULT_ACCOUNT_ID
                acct = q.get_account(aid)
                home_dir, _ = slot_mgr.dirs_for(aid)
                home_dir.mkdir(parents=True, exist_ok=True)
                proxy = slot_mgr.effective_proxy(acct["proxy_url"])
                extra_env = {
                    "GOOGLE_FLOW_LOGIN_ACCOUNT_ID": aid,
                    "GOOGLE_FLOW_LOGIN_HOME": str(home_dir),
                    "GOOGLE_FLOW_LOGIN_PROXY": proxy,
                }
                desktop_account_id = aid
            prev_aid = desktop_account_id
            result = json.loads(await command(hook, action, extra_env=extra_env))
            if action == "close":
                try:
                    slot_mgr.client_for(prev_aid).trigger_account_refresh()
                except Error:
                    pass
            if result.get("open"):
                result["account_id"] = desktop_account_id
            else:
                result["account_id"] = None
            return result

        if not no_gpu:
            lease = Lease(q, scheduler, identity, gpu, settle,
                          int(env.get("GOOGLE_FLOW_LEASE_RECONCILE_TIMEOUT_MINUTES", "30")))
        g = Gateway(q, None, Media(state / "cache"), default_output, org, slots=slot_mgr)
        g.admitted = lambda: (True if no_gpu else lease.valid) and slot_mgr.any_alive()
        net_url = env.get("GOOGLE_FLOW_NETWORK_URL", "").strip()
        net_tok = env.get("GOOGLE_FLOW_NETWORK_TOKEN", "").strip()
        proxies = NetworkProxies(net_url, net_tok) if (net_url and len(net_tok) >= 32) else None
        apps = [execution(g, execution_token), management(g, management_token, station, assets, lease, desktop, proxies)]
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

        async def start_slots():
            await command(hook, "start")
            await slot_mgr.sync_all()

        async def lifecycle():
            while not stop.is_set():
                if no_gpu:
                    await start_slots()
                elif not lease.valid:
                    if await lease.recover():
                        await start_slots()
                else:
                    await lease.heartbeat()
                    await slot_mgr.sync_all()
                await asyncio.sleep(30)

        async def work():
            next_cleanup = 0
            retention = int(env.get("GOOGLE_FLOW_CACHE_RETENTION_HOURS", "72"))
            if retention <= 0:
                raise ValueError("invalid_cache_retention")
            while True:
                if no_gpu or lease.valid:
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
                active = (True if no_gpu else lease.valid) and q.any_active_account()
                try:
                    await asyncio.to_thread(publisher.set_state, "enabled" if active else "disabled",
                                            "ready" if active else "paused")
                    await asyncio.to_thread(publisher.publish)
                except DiscoveryError:
                    if publisher.fenced:
                        raise
                await asyncio.sleep(15)

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        tasks.append(asyncio.create_task(lifecycle()))
        tasks.append(asyncio.create_task(work()))
        if publisher:
            tasks.append(asyncio.create_task(publish()))
        await stop.wait()
    finally:
        for server in servers:
            server.should_exit = True
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if slot_mgr is not None:
            await slot_mgr.stop_all()
        if publisher:
            await asyncio.to_thread(publisher.close)
        if lease is not None:
            await lease.release()
        for sock in sockets:
            sock.close()


def main():
    asyncio.run(run())


if __name__ == "__main__":
    main()
