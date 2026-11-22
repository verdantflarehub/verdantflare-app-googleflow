"""Multi-account slot manager: isolates Chrome profiles, gflow serve ports, and per-account egress proxies."""
import asyncio
import os
from pathlib import Path
import secrets
import shutil

from .gflow_client import GFlow, local_session
from .policy import Error
from .queue import DEFAULT_ACCOUNT_ID


class AccountSlot:
    def __init__(self, account_id, port, home_dir, output_dir, proxy_url, client, process=None):
        self.account_id = account_id
        self.port = port
        self.home_dir = Path(home_dir)
        self.output_dir = Path(output_dir)
        self.proxy_url = proxy_url or ""
        self.client = client
        self.process = process

    @property
    def alive(self):
        return self.process is not None and self.process.returncode is None


class AccountSlotManager:
    """Manages per-account gflow serve subprocesses and isolated home/output directories."""

    def __init__(self, queue, base_env, prices, default_home, default_output, accounts_root):
        self.q = queue
        self.base_env = dict(base_env)
        self.prices = dict(prices or {})
        self.default_home = Path(default_home)
        self.default_output = Path(default_output)
        self.accounts_root = Path(accounts_root)
        self.slots: dict[str, AccountSlot] = {}
        self._lock = asyncio.Lock()

    def dirs_for(self, account_id):
        if account_id == DEFAULT_ACCOUNT_ID:
            return self.default_home, self.default_output
        root = self.accounts_root / account_id
        return root / "home", root / "output"

    def effective_proxy(self, proxy_url):
        if proxy_url:
            return proxy_url
        return self.base_env.get("GFLOW_CLI_BROWSER_PROXY") or self.base_env.get("HTTPS_PROXY") or ""

    def get_slot(self, account_id):
        slot = self.slots.get(account_id)
        if slot is None:
            raise Error("account_not_found", 404, field="account_id")
        return slot

    def client_for(self, account_id=DEFAULT_ACCOUNT_ID):
        slot = self.slots.get(account_id)
        if slot is None:
            raise Error("channel_unavailable", 503)
        return slot.client

    def output_for(self, account_id=DEFAULT_ACCOUNT_ID):
        _, out_dir = self.dirs_for(account_id)
        return out_dir

    def any_alive(self):
        return any(slot.alive for slot in self.slots.values())

    async def sync_all(self):
        """Ensure every enabled account has a running gflow serve subprocess and stop disabled/removed ones."""
        async with self._lock:
            accts = {a["id"]: a for a in self.q.list_accounts()}
            # Stop removed or disabled slots (isolated per slot)
            for aid in list(self.slots):
                acct = accts.get(aid)
                if acct is None or not acct["enabled"]:
                    try:
                        await self._stop_slot_unlocked(aid)
                    except Exception:
                        pass
            # Start or restart enabled slots (isolate single-slot startup failures so healthy slots continue)
            for aid, acct in accts.items():
                if not acct["enabled"]:
                    continue
                slot = self.slots.get(aid)
                if slot is None or not slot.alive or slot.port != acct["port"] or slot.proxy_url != acct["proxy_url"]:
                    try:
                        if slot is not None:
                            await self._stop_slot_unlocked(aid)
                        await self._start_slot_unlocked(acct)
                    except Exception:
                        pass

    async def restart_slot(self, account_id):
        async with self._lock:
            await self._stop_slot_unlocked(account_id)
            acct = self.q.get_account(account_id)
            if acct["enabled"]:
                await self._start_slot_unlocked(acct)

    async def stop_all(self):
        async with self._lock:
            for aid in list(self.slots):
                await self._stop_slot_unlocked(aid)

    def cleanup_deleted_dirs(self, account_id):
        if account_id == DEFAULT_ACCOUNT_ID:
            return
        root = self.accounts_root / account_id
        if root.exists():
            shutil.rmtree(root, ignore_errors=True)

    async def _stop_slot_unlocked(self, account_id):
        slot = self.slots.get(account_id)
        if slot and slot.alive:
            slot.process.terminate()
            try:
                await asyncio.wait_for(slot.process.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                slot.process.kill()
                await slot.process.wait()
        self.slots.pop(account_id, None)

    async def _start_slot_unlocked(self, acct):
        aid = acct["id"]
        home_dir, output_dir = self.dirs_for(aid)
        home_dir.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)
        if aid != DEFAULT_ACCOUNT_ID:
            cfg = home_dir / "config.toml"
            if not cfg.exists():
                cfg.write_text('default_profile = "default"\n', encoding="utf-8")
            prof = home_dir / "profile_default"
            prof.mkdir(parents=True, exist_ok=True)
            marker = prof / ".gflow_browser_strategy"
            if not marker.exists():
                marker.write_text("chrome", encoding="utf-8")
        token = secrets.token_urlsafe(48)
        allowed = {"PATH", "HOME", "DO_NOT_TRACK", "LANG", "LC_ALL", "DISPLAY", "WAYLAND_DISPLAY",
                   "DBUS_SESSION_BUS_ADDRESS", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY"}
        process_env = {k: v for k, v in self.base_env.items()
                       if k.upper() in allowed or k.startswith(("XDG_", "GFLOW_CLI_"))}
        proxy = self.effective_proxy(acct["proxy_url"])
        process_env.update(
            GFLOW_CLI_HOME=str(home_dir),
            GFLOW_CLI_OUTPUT_DIR=str(output_dir),
            GFLOW_CLI_DAEMON_TOKEN=token,
            GFLOW_CLI_TELEMETRY="0",
            GFLOW_CLI_UPDATE_CHECK="false",
            GFLOW_CLI_INCIDENT_CAPTURE="false",
            GFLOW_CLI_LEASE_WAIT_SECONDS=str(self.base_env.get("GFLOW_CLI_LEASE_WAIT_SECONDS", "30")),
        )
        if proxy:
            process_env["GFLOW_CLI_BROWSER_PROXY"] = proxy
            process_env["HTTPS_PROXY"] = proxy
            process_env["https_proxy"] = proxy
            process_env["HTTP_PROXY"] = proxy
            process_env["http_proxy"] = proxy
        proc = await asyncio.create_subprocess_exec(
            "gflow", "serve", "--host", "127.0.0.1", "--port", str(acct["port"]),
            env=process_env, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        client = GFlow(
            local_session(token, port=acct["port"]),
            self.prices,
            initial_snapshot=self.q.load_account_snapshot(aid),
            on_snapshot=lambda snap, _aid=aid: self.q.save_account_snapshot(_aid, snap),
        )
        slot = AccountSlot(aid, acct["port"], home_dir, output_dir, acct["proxy_url"], client, proc)
        self.slots[aid] = slot

        async def _warmup():
            await asyncio.sleep(3)
            if slot.alive:
                client.trigger_account_refresh()
        asyncio.create_task(_warmup())
