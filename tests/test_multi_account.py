"""Multi-account scheduling, migration, proxy validation, and fault-isolation tests (S1)."""
import json
import sqlite3
import tempfile
from pathlib import Path
import unittest
from starlette.testclient import TestClient

from google_flow_gateway.management import management
from google_flow_gateway.policy import DEFAULTS, Error, validate_proxy_url
from google_flow_gateway.queue import DEFAULT_ACCOUNT_ID, Queue
from google_flow_gateway.runtime import kasmvnc_has_client
from google_flow_gateway.server import Gateway, execution


class FakeAccountClient:
    def __init__(self, account_id, credits=100, authorized=True, email=None):
        self.account_id = account_id
        self.credits = credits
        self.authorized = authorized
        self.email = email or f"{account_id}@example.com"
        self.calls = []
        self.mode = "ok"

    async def account(self):
        return {
            "authorized": self.authorized,
            "credits": self.credits,
            "email": self.email,
            "updated_at": 1700000000,
        }

    async def cached_account(self):
        return await self.account()

    async def catalog(self):
        return {"models": {"video": ["veo-lite", "veo-fast", "veo-quality", "omni-1.2-flash"],
                           "image": ["nano2", "nano21", "nano-pro"]}}

    def quote(self, payload):
        return 10 if payload.get("capability") == "video" else 0

    async def tool(self, name, **arguments):
        self.calls.append((name, arguments))
        if self.mode == "unusual_activity":
            return {"error": {"code": "unusual_activity", "detail": "PUBLIC_ERROR_UNUSUAL_ACTIVITY", "accepted": False, "charged": "no"}}
        if self.mode == "upload_terms":
            return {"error": {
                "type": "https://gflow-cli.dev/errors/media-upload-rejected",
                "title": "Flow rejected a media upload",
                "detail": "migrated host: a dialog opened after the file was chosen — most likely Flow's one-time upload-terms confirmation",
                "charged": "no",
            }}
        if self.mode == "authorization_required":
            raise Error("authorization_required", 401)
        if name == "gflow_generate_video":
            return {"flow_media_id": f"m-{self.account_id}-{len(self.calls)}", "cost": 10}
        if name == "gflow_generate_image":
            return {"flow_media_id": f"img-{self.account_id}-{len(self.calls)}", "cost": 0}
        if name == "gflow_download_media":
            return {"path": None}
        return {}


class MultiAccountTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.now = 1_700_000_000.0
        self.q = Queue(self.root / "state.db", lambda: self.now)
        self.media = self.root / "media"
        self.media.mkdir()
        self.output = self.root / "output"
        self.output.mkdir()
        self.clients = {
            "default": FakeAccountClient("default", credits=100, email="main@example.com"),
        }
        self.gw = Gateway(self.q, self.clients["default"], self.media, self.output, "org1", slots=self.clients)
        self.token = "tok-" + "a" * 32
        self.mgmt_token = "mgmt-" + "b" * 32
        self.api = TestClient(execution(self.gw, self.token))
        self.mgmt = TestClient(management(self.gw, self.mgmt_token, "station-1"))
        self.api_headers = {
            "Authorization": f"Bearer {self.token}",
            "X-User-Id": "u1",
            "X-Organization-Id": "org1",
            "X-Project-Id": "p1",
            "X-Project-Write": "true",
        }
        self.mgmt_headers = {
            "Authorization": f"Bearer {self.mgmt_token}",
            "X-User-Id": "admin",
            "X-Organization-Id": "org1",
            "X-Station-Id": "station-1",
        }

    def tearDown(self):
        self.q.db.close()
        self.tmp.cleanup()

    def test_legacy_single_account_migration(self):
        legacy_db = self.root / "legacy.db"
        conn = sqlite3.connect(legacy_db)
        conn.executescript("""
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE calls (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, key TEXT NOT NULL UNIQUE,
                payload TEXT NOT NULL, state TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
                accepted INTEGER, reason TEXT, media_id TEXT, reserve INTEGER NOT NULL DEFAULT 0,
                spent INTEGER NOT NULL DEFAULT 0, epoch INTEGER NOT NULL DEFAULT 0,
                started REAL, attempt INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE audit (id INTEGER PRIMARY KEY AUTOINCREMENT, time REAL NOT NULL,
                actor TEXT NOT NULL, action TEXT NOT NULL, detail TEXT NOT NULL);
        """)
        custom_policy = DEFAULTS | {"min_interval_seconds": 45, "concurrency": 2}
        conn.execute("INSERT INTO settings VALUES('policy', ?)", (json.dumps(custom_policy),))
        conn.execute("INSERT INTO settings VALUES('pause', ?)", (json.dumps({"reason": "manual", "since": 100, "until": None}),))
        conn.execute("INSERT INTO settings VALUES('epoch', '3')")
        conn.execute(
            "INSERT INTO calls(id,owner,key,payload,state,accepted,spent,epoch,started) VALUES('c1','u1','k1','{}','succeeded',1,10,3,1700000000)"
        )
        conn.commit()
        conn.close()

        q2 = Queue(legacy_db, lambda: self.now)
        try:
            accts = q2.list_accounts()
            self.assertEqual(len(accts), 1)
            self.assertEqual(accts[0]["id"], DEFAULT_ACCOUNT_ID)
            self.assertEqual(q2.account_policy(DEFAULT_ACCOUNT_ID)["min_interval_seconds"], 45)
            self.assertEqual(q2.status(DEFAULT_ACCOUNT_ID)["status"], "paused")
            self.assertEqual(q2.status(DEFAULT_ACCOUNT_ID)["reason"], "manual")
            row = q2.find("id", "c1")
            self.assertEqual(row["account_id"], DEFAULT_ACCOUNT_ID)
        finally:
            q2.db.close()

    def test_proxy_url_validation_and_account_crud_lifecycle(self):
        # Valid proxy URLs (default allowlist includes openclash.openclash.svc.cluster.local)
        self.assertEqual(validate_proxy_url(None), "")
        self.assertEqual(validate_proxy_url(""), "")
        self.assertEqual(
            validate_proxy_url("http://openclash.openclash.svc.cluster.local:1083"),
            "http://openclash.openclash.svc.cluster.local:1083",
        )
        # Invalid proxy URLs (including malformed port that triggers urlsplit ValueError)
        for bad in [
            "socks5://openclash.openclash.svc.cluster.local:1083",
            "http://evil.example.com:1083",
            "http://user:pass@openclash.openclash.svc.cluster.local:1083",
            "http://openclash.openclash.svc.cluster.local:80",
            "http://openclash.openclash.svc.cluster.local:99999",
            "http://openclash.openclash.svc.cluster.local:abc",
            "http://openclash.openclash.svc.cluster.local:1083/path",
        ]:
            with self.assertRaises(Error):
                validate_proxy_url(bad)

        # Create second account via management API
        r = self.mgmt.post("/accounts", headers=self.mgmt_headers, json={
            "label": "备用 Ultra", "priority": 2, "proxy_url": "http://openclash.openclash.svc.cluster.local:1083",
        })
        self.assertEqual(r.status_code, 201, r.text)
        aid = r.json()["id"]
        self.assertTrue(aid.startswith("acct_"))
        self.assertEqual(r.json()["port"], 18002)

        # Reject invalid proxy via API
        r_bad = self.mgmt.post("/accounts", headers=self.mgmt_headers, json={
            "label": "Bad", "proxy_url": "http://1.2.3.4:1083",
        })
        self.assertEqual(r_bad.status_code, 422)

        # Cannot delete default account
        r_del_def = self.mgmt.delete("/accounts/default", headers=self.mgmt_headers)
        self.assertEqual(r_del_def.status_code, 409)

        # Cannot delete enabled account
        r_del_en = self.mgmt.delete(f"/accounts/{aid}", headers=self.mgmt_headers)
        self.assertEqual(r_del_en.status_code, 409)

        # Reject proxy_url update when account has inflight call
        self.clients[aid] = FakeAccountClient(aid, credits=50)
        row = self.q.intent("u1", "k-busy", {
            "model": "veo-lite", "capability": "video", "parameters": {"prompt": "hi"},
            "channel_account_id": aid,
        })
        self.assertEqual(self.q.admit_multi(row["id"], 10, {aid: 50}), aid)
        r_busy = self.mgmt.put(f"/accounts/{aid}", headers=self.mgmt_headers, json={
            "proxy_url": "http://openclash.openclash.svc.cluster.local:1084",
        })
        self.assertEqual(r_busy.status_code, 409)
        self.assertEqual(r_busy.json()["code"], "account_busy")

        # Complete call, disable, then delete
        self.q.outcome(row["id"], True, media="m1", spent=10)
        r_dis = self.mgmt.put(f"/accounts/{aid}", headers=self.mgmt_headers, json={
            "enabled": False, "proxy_url": "http://openclash.openclash.svc.cluster.local:1084",
        })
        self.assertEqual(r_dis.status_code, 200)
        self.assertEqual(r_dis.json()["proxy_url"], "http://openclash.openclash.svc.cluster.local:1084")
        r_del = self.mgmt.delete(f"/accounts/{aid}", headers=self.mgmt_headers)
        self.assertEqual(r_del.status_code, 200)

    def test_auto_select_priority_and_load_balancing(self):
        # Update default priority to 10 so we can test explicit priorities cleanly
        self.q.update_account("default", {"priority": 10}, "admin")
        a2 = self.q.create_account({"label": "高优账号", "priority": 10}, "admin")["id"]
        a3 = self.q.create_account({"label": "低优账号", "priority": 20}, "admin")["id"]
        self.clients[a2] = FakeAccountClient(a2, credits=500)
        self.clients[a3] = FakeAccountClient(a3, credits=1000)

        # Auto-select picks a2 first (priority=10, running=0, last_started=0, credits 500 > default 100)
        r1 = self.q.intent("u1", "k-auto-1", {"model": "veo-lite", "capability": "video", "parameters": {"prompt": "a"}})
        chosen1 = self.q.admit_multi(r1["id"], 10, {"default": 100, a2: 500, a3: 1000})
        self.assertEqual(chosen1, a2)

        # While a2 is running (concurrency=1), next call spills over to default (priority=10, running=0)
        r2 = self.q.intent("u1", "k-auto-2", {"model": "veo-lite", "capability": "video", "parameters": {"prompt": "b"}})
        chosen2 = self.q.admit_multi(r2["id"], 10, {"default": 100, a2: 500, a3: 1000})
        self.assertEqual(chosen2, "default")

        # While both priority=10 accounts are busy, third call spills over to a3 (priority=20)
        r3 = self.q.intent("u1", "k-auto-3", {"model": "veo-lite", "capability": "video", "parameters": {"prompt": "c"}})
        chosen3 = self.q.admit_multi(r3["id"], 10, {"default": 100, a2: 500, a3: 1000})
        self.assertEqual(chosen3, a3)

    def test_explicit_channel_account_id_strict_binding(self):
        a2 = self.q.create_account({"label": "指定账号", "priority": 500}, "admin")["id"]
        self.clients[a2] = FakeAccountClient(a2, credits=100)

        # Explicit binding routes to a2 even though default has higher priority
        r = self.q.intent("u1", "k-exp-1", {
            "model": "veo-lite", "capability": "video", "parameters": {"prompt": "x"},
            "channel_account_id": a2,
        })
        chosen = self.q.admit_multi(r["id"], 10, {"default": 100, a2: 100})
        self.assertEqual(chosen, a2)

        # Pause a2 -> explicit call to a2 must be rejected (no failover to default)
        self.q.action("pause", "admin", account_id=a2)
        with self.assertRaises(Error) as ctx:
            self.q.intent("u1", "k-exp-2", {
                "model": "veo-lite", "capability": "video", "parameters": {"prompt": "y"},
                "channel_account_id": a2,
            })
        self.assertEqual(ctx.exception.code, "channel_paused")

    async def test_single_account_fault_isolation_and_filters(self):
        self.q.update_account("default", {"priority": 10}, "admin")
        a2 = self.q.create_account({"label": "稳健账号", "priority": 20}, "admin")["id"]
        self.clients[a2] = FakeAccountClient(a2, credits=200)
        self.clients["default"].mode = "unusual_activity"

        # First call hits default (priority=10) -> triggers 403 unusual_activity -> pauses ONLY default
        r1 = self.api.post("/v1/calls", headers=self.api_headers, json={
            "idempotency_key": "k-iso-1", "model": "veo-lite", "capability": "video",
            "parameters": {"prompt": "hello"},
        })
        self.assertEqual(r1.status_code, 200, r1.text)
        self.assertEqual(r1.json()["state"], "failed")
        self.assertEqual(r1.json()["account_id"], "default")
        self.assertEqual(self.q.status("default")["status"], "paused")
        self.assertEqual(self.q.status(a2)["status"], "active")

        # Second call (auto) automatically routes to a2 and is accepted!
        r2 = self.api.post("/v1/calls", headers=self.api_headers, json={
            "idempotency_key": "k-iso-2", "model": "veo-lite", "capability": "video",
            "parameters": {"prompt": "world"},
        })
        self.assertEqual(r2.status_code, 200, r2.text)
        self.assertTrue(r2.json()["accepted"])
        self.assertIn(r2.json()["state"], ("running", "succeeded"))
        self.assertEqual(r2.json()["account_id"], a2)

        # /calls?account_id= and /audit?account_id= filters
        c_def = self.mgmt.get("/calls?account_id=default", headers=self.mgmt_headers).json()
        c_b = self.mgmt.get(f"/calls?account_id={a2}", headers=self.mgmt_headers).json()
        self.assertEqual(len(c_def), 1)
        self.assertEqual(c_def[0]["account_id"], "default")
        self.assertEqual(len(c_b), 1)
        self.assertEqual(c_b[0]["account_id"], a2)

        # Per-account policy update does not leak to other accounts
        p_res = self.mgmt.put(f"/accounts/{a2}/policy", headers=self.mgmt_headers, json={"min_interval_seconds": 99})
        self.assertEqual(p_res.status_code, 200)
        self.assertEqual(self.q.account_policy(a2)["min_interval_seconds"], 99)
        self.assertNotEqual(self.q.account_policy("default")["min_interval_seconds"], 99)

        # /v1/account aggregates credits from active accounts
        acct_agg = self.api.get("/v1/account", headers=self.api_headers).json()
        self.assertTrue(acct_agg["authorized"])
        self.assertEqual(acct_agg["credits"], 200)
        self.assertEqual(len(acct_agg["accounts"]), 2)

    def test_kasmvnc_has_client_hex_port_48083_ipv4_ipv6(self):
        # 48083 == 0xBBD3; listening socket (0A) and wrong port 48091 (BBDB) must NOT match
        tcp4 = self.root / "tcp"
        tcp6 = self.root / "tcp6"
        header = "  sl  local_address rem_address   st tx_queue rx_queue\n"
        tcp4.write_text(header + "   0: 00000000:BBD3 00000000:0000 0A 00:00\n   1: 0100007F:BBDB 0100007F:1234 01 00:00\n")
        tcp6.write_text(header)
        self.assertFalse(kasmvnc_has_client(48083, (tcp4, tcp6)))
        # IPv4 ESTABLISHED (01) on 48083 (BBD3) matches
        tcp4.write_text(header + "   0: 00000000:BBD3 00000000:0000 0A 00:00\n   1: 2600020A:BBD3 0100020A:C350 01 00:00\n")
        self.assertTrue(kasmvnc_has_client(48083, (tcp4, tcp6)))
        # IPv6 ESTABLISHED (01) on 48083 (BBD3) matches
        tcp4.write_text(header)
        tcp6.write_text(header + "   0: 00000000000000000000000000000001:BBD3 00000000000000000000000000000001:C350 01 00:00\n")
        self.assertTrue(kasmvnc_has_client(48083, (tcp4, tcp6)))

    async def test_account_snapshot_persists_across_restart(self):
        import asyncio
        from google_flow_gateway.gflow_client import GFlow

        # Save snapshot into SQLite
        snap = {"authorized": True, "credits": 25040, "email": "main@example.com", "tier": "ULTRA", "updated_at": 1700000000.0}
        self.q.save_account_snapshot("default", snap)

        # Simulate process restart: reopen Queue and instantiate GFlow with persisted snapshot + slow session
        q2 = Queue(self.root / "state.db", lambda: self.now)
        try:
            loaded = q2.load_account_snapshot("default")
            self.assertEqual(loaded["credits"], 25040)
            self.assertEqual(loaded["email"], "main@example.com")

            refresh_started = asyncio.Event()

            class SlowGFlow(GFlow):
                async def account(self):
                    refresh_started.set()
                    await asyncio.sleep(5)
                    return snap

            client = SlowGFlow(None, initial_snapshot=loaded, on_snapshot=lambda s: q2.save_account_snapshot("default", s))
            # cached_account must return the persisted snapshot immediately instead of authorized=None
            got = await client.cached_account(ttl=60.0, max_wait=0.05)
            self.assertTrue(got["authorized"])
            self.assertEqual(got["credits"], 25040)
            self.assertEqual(got["email"], "main@example.com")
            await asyncio.sleep(0)
            self.assertTrue(refresh_started.is_set())
            if client._account_refresh_task:
                client._account_refresh_task.cancel()
        finally:
            q2.db.close()

    async def test_upload_terms_required_pauses_only_faulty_account(self):
        self.q.update_account("default", {"priority": 10}, "admin")
        a2 = self.q.create_account({"label": "备用账号", "priority": 20}, "admin")["id"]
        self.clients[a2] = FakeAccountClient(a2, credits=200)
        self.clients["default"].mode = "upload_terms"

        r1 = self.api.post("/v1/calls", headers=self.api_headers, json={
            "idempotency_key": "k-upload-1", "model": "veo-lite", "capability": "video",
            "parameters": {"prompt": "hello"},
        })
        self.assertEqual(r1.status_code, 200, r1.text)
        self.assertEqual(r1.json()["state"], "failed")
        self.assertEqual(r1.json()["reason_code"], "upload_terms_required")
        self.assertEqual(self.q.status("default")["status"], "paused")
        self.assertEqual(self.q.status("default")["reason"], "upload_terms_required")
        self.assertEqual(self.q.status(a2)["status"], "active")

        # Next auto-routed call skips paused default and succeeds on a2
        r2 = self.api.post("/v1/calls", headers=self.api_headers, json={
            "idempotency_key": "k-upload-2", "model": "veo-lite", "capability": "video",
            "parameters": {"prompt": "world"},
        })
        self.assertEqual(r2.status_code, 200, r2.text)
        self.assertTrue(r2.json()["accepted"])
        self.assertEqual(r2.json()["account_id"], a2)

    async def test_slot_injects_lease_wait_seconds_and_snapshot(self):
        from unittest.mock import AsyncMock, patch
        from google_flow_gateway.slots import AccountSlotManager

        self.q.save_account_snapshot("default", {"authorized": True, "credits": 999, "email": "u@example.com", "updated_at": 1.0})
        mgr = AccountSlotManager(self.q, {}, {}, self.root / "gflow", self.root / "out", self.root / "accts")
        captured_env = {}

        class DummyProc:
            returncode = None
            def terminate(self): self.returncode = 0
            async def wait(self): return 0

        async def fake_exec(*args, **kwargs):
            captured_env.update(kwargs.get("env", {}))
            return DummyProc()

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            await mgr.sync_all()
            self.assertEqual(captured_env.get("GFLOW_CLI_LEASE_WAIT_SECONDS"), "30")
            slot = mgr.get_slot("default")
            self.assertEqual(slot.client._account_cache["credits"], 999)
            await mgr.stop_all()

    async def test_v1_account_and_dispatch_use_cached_account_without_blocking(self):
        import time

        class CachedFastClient(FakeAccountClient):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.live_calls = 0
                self.cached_calls = 0
                self.refresh_triggered = 0

            async def account(self):
                self.live_calls += 1
                raise AssertionError("live blocking account() should not be called when cache is populated")

            async def cached_account(self):
                self.cached_calls += 1
                return {"authorized": self.authorized, "credits": self.credits, "email": self.email, "updated_at": 1700000000}

            def trigger_account_refresh(self):
                self.refresh_triggered += 1

        a2 = self.q.create_account({"label": "备用账号", "priority": 20}, "admin")["id"]
        c1 = CachedFastClient("default", credits=100)
        c2 = CachedFastClient(a2, credits=200)
        self.clients["default"] = c1
        self.clients[a2] = c2

        t0 = time.monotonic()
        r_acct = self.api.get("/v1/account", headers=self.api_headers)
        self.assertEqual(r_acct.status_code, 200, r_acct.text)
        self.assertLess(time.monotonic() - t0, 1.0)
        body = r_acct.json()
        self.assertTrue(body["authorized"])
        self.assertEqual(body["credits"], 300)
        self.assertEqual(c1.live_calls, 0)
        self.assertEqual(c2.live_calls, 0)
        self.assertEqual(c1.cached_calls, 1)
        self.assertEqual(c2.cached_calls, 1)

        r_call = self.api.post("/v1/calls", headers=self.api_headers, json={
            "idempotency_key": "k-cached-fast-1", "model": "veo-lite", "capability": "video",
            "parameters": {"prompt": "fast cached dispatch"},
        })
        self.assertEqual(r_call.status_code, 200, r_call.text)
        self.assertTrue(r_call.json()["accepted"])
        self.assertEqual(c1.live_calls, 0)
        self.assertEqual(c2.live_calls, 0)
        self.assertGreaterEqual(c1.refresh_triggered + c2.refresh_triggered, 1)


if __name__ == "__main__":
    unittest.main()
