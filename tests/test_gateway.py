import asyncio
from contextlib import asynccontextmanager
import hashlib
import io
import json
import logging
import subprocess
from pathlib import Path
import secrets
import tempfile
import unittest

import httpx
from mcp.client import Client
from mcp.server import MCPServer
from PIL import Image

from google_flow_gateway.dashboard import DashboardAssets, build
from google_flow_gateway import __version__
from google_flow_gateway.gflow_client import GFlow
from google_flow_gateway.gpu_lease import Lease
from google_flow_gateway.management import management
from google_flow_gateway.media import Media
from google_flow_gateway.policy import DEFAULTS, Error
from google_flow_gateway.queue import Queue
from google_flow_gateway.server import Gateway, execution

logging.getLogger("httpx").setLevel(logging.WARNING)


class Clock:
    def __init__(self):
        self.now = 123456
    def __call__(self):
        return self.now


class FakeFlow:
    def __init__(self, root):
        self.mcp = MCPServer("fixture")
        self.authorized = True
        self.mode = "success"
        self.count = self.reads = self.total = self.active = self.peak = 0
        self.gate = asyncio.Event()
        self.gate.set()
        self.entered = asyncio.Queue()
        self.path = root / "result.png"
        Image.new("RGB", (4, 4)).save(self.path)
        self.price = 3

        @self.mcp.tool()
        def gflow_auth_status() -> dict:
            self.total += 1
            return {"status": "authenticated" if self.authorized else "unauthenticated"}

        @self.mcp.tool()
        def gflow_get_credits() -> dict:
            return {"credits": 10000}

        @self.mcp.tool()
        def gflow_list_models() -> dict:
            return {"image": {"models": [{"aliases": ["nano2", "unlisted"]}]},
                    "video": {"models": [{"aliases": ["veo-fast"]}]}}

        @self.mcp.tool()
        def gflow_list_tools() -> dict:
            return {"tools": []}

        @self.mcp.tool()
        async def gflow_generate_image(prompt: str, model: str) -> dict:
            self.count += 1
            self.active += 1
            self.peak = max(self.peak, self.active)
            await self.entered.put(True)
            try:
                await self.gate.wait()
                if self.mode == "unknown":
                    return {"status": "unrecognized"}
                if self.mode == "403":
                    return {"error": {"code": "UNUSUAL_ACTIVITY", "status": 403, "charged": "no"}}
                return {"status": "completed", "flow_media_id": "asset", "files": [str(self.path)], "credits_used": self.price}
            finally:
                self.active -= 1

        @self.mcp.tool()
        def gflow_download_media(media_id: str) -> dict:
            self.reads += 1
            return {"path": str(self.path), "media_id": media_id, "credits_used": self.price}

        @self.mcp.tool()
        def gflow_generate_video(prompt: str, model: str) -> dict:
            self.count += 1
            return {"status": "completed", "flow_media_id": "video-asset",
                    "files": [str(root / "fixture.mp4")], "credits_used": self.price}

        @self.mcp.tool()
        def gflow_upscale_image(media_id: str, scale: str = "2k") -> dict:
            if self.mode == "upscale_error":
                raise RuntimeError("upscale_failed")
            if self.mode == "upscale_missing":
                return {"path": str(root / "nonexistent-upscaled.png")}
            up_path = root / f"result_{scale}.png"
            Image.new("RGB", (8, 8)).save(up_path)
            return {"path": str(up_path), "media_id": media_id, "scale": scale}

        @self.mcp.tool()
        def gflow_upscale_video(media_id: str, scale: str = "1080p", project: str | None = None) -> dict:
            if self.mode == "upscale_error":
                raise RuntimeError("upscale_failed")
            if self.mode == "upscale_missing":
                return {"path": str(root / "nonexistent-upscaled.mp4")}
            return {"path": str(root / "fixture.mp4"), "media_id": media_id, "scale": scale}


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        base = Path(".test-work")
        base.mkdir(exist_ok=True)
        tmp = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.clock = Clock()
        self.q = Queue(self.root / "q.sqlite", self.clock)
        self.addCleanup(lambda: self.q.db.close())
        self.fake = FakeFlow(self.root)
        self.g = Gateway(self.q, GFlow(lambda: Client(self.fake.mcp), {"nano2": 3}),
                         Media(self.root / "cache"), self.root, "org")
        token, admin = secrets.token_hex(32), secrets.token_hex(32)
        self.x = httpx.AsyncClient(transport=httpx.ASGITransport(app=execution(self.g, token)), base_url="http://test",
              headers={"Authorization": "Bearer " + token, "x-organization-id": "org", "x-project-id": "p",
                       "x-user-id": "user", "x-project-write": "true"})
        self.m = httpx.AsyncClient(transport=httpx.ASGITransport(app=management(self.g, admin, "station")), base_url="http://test",
              headers={"Authorization": "Bearer " + admin, "x-organization-id": "org",
                       "x-user-id": "operator", "x-station-id": "station"})
        self.addAsyncCleanup(self.x.aclose)
        self.addAsyncCleanup(self.m.aclose)

    def payload(self, key, path="/v1/calls"):
        return {"idempotency_key" if path == "/v1/calls" else "request_id": key,
                "model": "nano2", "capability": "image", "parameters": {"prompt": "test"}}

    async def submit(self, key, path="/v1/calls"):
        res = await self.x.post(path, json=self.payload(key, path))
        self.assertEqual(res.status_code, 200, res.text)
        return res.json()

    async def config(self, **changes):
        self.assertEqual((await self.m.put("/policy", json=changes)).status_code, 200)

    async def test_defaults_and_invalid_fields(self):
        self.assertEqual((await self.m.get("/policy")).json(), {
            "concurrency": 1, "min_interval_seconds": 0, "daily_submit_limit": 0, "per_job_credit_limit": 0,
            "daily_credit_limit": 0, "on_unusual_activity": "pause", "cooldown_minutes": 0, "on_submission_unknown": "hold"})
        for field, value in [("concurrency", 0), ("concurrency", -1), ("on_submission_unknown", 0), ("on_unusual_activity", 0)]:
            with self.subTest(field=field, value=value):
                res = await self.m.put("/policy", json={field: value})
                self.assertEqual((res.status_code, res.json()["field"]), (422, field))
        self.assertEqual((await self.m.get("/policy")).json(), DEFAULTS)

    async def test_dual_listener_scope(self):
        self.assertEqual((await self.x.get("/policy")).status_code, 404)
        self.assertEqual((await self.m.get("/v1/models")).status_code, 404)
        self.assertEqual((await self.m.get("/overview", headers={"authorization": self.x.headers["authorization"]})).status_code, 401)
        self.assertEqual((await self.x.get("/v1/models", headers={"authorization": self.m.headers["authorization"]})).status_code, 401)

    async def test_interval_thresholds_hot_reload(self):
        for limit in (5, 11):
            with self.subTest(limit=limit):
                await self.config(min_interval_seconds=0)
                await self.submit(f"start-{limit}")
                await self.config(min_interval_seconds=limit)
                self.clock.now += limit - 1
                key = f"b-{limit}"
                self.assertEqual((await self.submit(key))["state"], "pending")
                self.clock.now += 1
                self.assertEqual((await self.submit(key))["state"], "succeeded")
                self.clock.now += limit + 1
                self.assertEqual((await self.submit(f"after-{limit}"))["state"], "succeeded")
                await self.config(min_interval_seconds=0)
                self.assertEqual((await self.submit(f"zero-{limit}"))["state"], "succeeded")

    async def test_daily_submit_thresholds_hot_reload(self):
        for limit in (2, 3):
            await self.m.post("/channel/reset-count")
            await self.config(daily_submit_limit=limit)
            for n in range(limit):
                self.assertEqual((await self.submit(f"{limit}-{n}"))["state"], "succeeded")
            self.assertEqual((await self.submit(f"x-{limit}"))["state"], "pending")
            await self.config(daily_submit_limit=0)
            self.assertEqual((await self.submit(f"x-{limit}"))["state"], "succeeded")

    async def test_per_job_credit_thresholds_hot_reload(self):
        for limit in (3, 6):
            await self.config(per_job_credit_limit=limit)
            for amount in (limit-1, limit, limit+1):
                self.g.client.prices["nano2"] = self.fake.price = amount
                state = (await self.submit(f"{limit}-{amount}"))["state"]
                self.assertEqual(state, "succeeded" if amount <= limit else "failed")
            await self.config(per_job_credit_limit=0)
            self.assertEqual((await self.submit(f"zero-{limit}"))["state"], "succeeded")

    async def test_daily_credit_thresholds_hot_reload(self):
        for limit in (4, 7):
            await self.m.post("/channel/reset-count")
            await self.config(daily_credit_limit=limit)
            for n, amount in enumerate((limit-1, 1, 1)):
                self.g.client.prices["nano2"] = self.fake.price = amount
                self.assertEqual((await self.submit(f"{limit}-{n}"))["state"], "succeeded" if n<2 else "failed")
            self.assertEqual(self.q.status()["reason"], "credit_limit")
            await self.config(daily_credit_limit=0)
            await self.m.post("/channel/resume")
            self.assertEqual((await self.submit(f"zero-{limit}"))["state"], "succeeded")

    async def test_cross_entry_concurrency_barrier(self):
        for limit in (1, 2):
            await self.config(concurrency=limit)
            self.fake.peak = 0
            self.fake.gate.clear()
            tasks = []
            try:
                for n in range(limit):
                    path = "/v1/jobs" if n % 2 else "/v1/calls"
                    tasks.append(asyncio.create_task(self.submit(f"run-{limit}-{n}", path)))
                    await asyncio.wait_for(self.fake.entered.get(), 4)
                self.assertEqual((await self.submit(f"wait-{limit}", "/v1/jobs"))["state"], "pending")
                self.assertEqual(self.fake.peak, limit)
            finally:
                self.fake.gate.set()
                await asyncio.gather(*tasks)

    async def test_cross_entry_budget_and_failure_release(self):
        await self.config(concurrency=2, daily_credit_limit=5, on_unusual_activity="continue")
        self.fake.gate.clear()
        self.fake.mode = "403"
        task = asyncio.create_task(self.submit("first"))
        try:
            await asyncio.wait_for(self.fake.entered.get(), 4)
            self.assertEqual((await self.submit("second", "/v1/jobs"))["state"], "failed")
            self.assertEqual(self.q.usage()["reserved"], 3)
            self.assertEqual(self.fake.count, 1)
        finally:
            self.fake.gate.set()
            await task
        self.assertEqual(self.q.usage(), {"reserved": 0, "submits": 0, "credits": 0})

    async def test_unusual_activity_actions_and_cooldown(self):
        self.fake.mode = "403"
        await self.config(on_unusual_activity="continue")
        await self.submit("continue")
        self.assertEqual(self.q.status()["status"], "active")
        await self.config(on_unusual_activity="pause")
        await self.submit("pause")
        self.clock.now += 9000
        self.assertEqual(self.q.status()["reason"], "unusual_activity")
        await self.m.post("/channel/resume")
        await self.config(cooldown_minutes=2)
        await self.submit("cool")
        self.clock.now += 119
        self.assertEqual(self.q.status()["status"], "paused")
        self.clock.now += 1
        self.assertEqual(self.q.status()["status"], "active")

    async def test_manual_resume_all_reasons_and_audit(self):
        for reason in ("manual", "unusual_activity", "credit_limit", "submission_unknown", "authorization_required"):
            with self.q.transaction():
                self.q.pause(reason)
            res = await self.m.post("/channel/resume")
            self.assertEqual(res.status_code, 200)
            log = (await self.m.get("/audit")).json()[0]
            self.assertEqual((log["actor"], log["detail"]["old"]["reason"], log["detail"]["new"]),
                             ("operator", reason, "active"))
        self.fake.authorized = False
        self.assertEqual((await self.x.post("/v1/calls", json=self.payload("noauth"))).status_code, 403)
        self.assertEqual(self.q.status()["reason"], "authorization_required")
        await self.m.post("/channel/resume")
        self.assertEqual((await self.x.post("/v1/calls", json=self.payload("again"))).status_code, 403)
        self.assertEqual(self.fake.count, 0)

    async def test_clear_cooldown_and_counter_reset_keep_history(self):
        await self.submit("keep")
        await self.config(cooldown_minutes=1)
        self.fake.mode = "403"
        await self.submit("pause")
        await self.m.post("/channel/clear-cooldown")
        self.clock.now += 120
        self.assertIsNone(self.q.status()["until"])
        self.assertEqual(self.q.status()["status"], "paused")
        self.assertEqual(self.q.usage()["credits"], 3)
        await self.m.post("/channel/reset-count")
        self.assertEqual(self.q.usage()["credits"], 0)
        self.assertEqual(len((await self.m.get("/calls")).json()), 2)
        actions = [r["action"] for r in (await self.m.get("/audit")).json()]
        self.assertIn("succeeded", actions)
        self.assertIn("clear-cooldown", actions)
        self.assertIn("reset-count", actions)

    async def test_idempotency_replay_conflict_and_project_access(self):
        call = await self.submit("same")
        for path in ("/v1/jobs", "/v1/calls"):
            self.assertEqual((await self.submit("same", path))["call_id"], call["call_id"])
            data = self.payload("same", path)
            data["parameters"]["prompt"] = "different"
            self.assertEqual((await self.x.post(path, json=data)).status_code, 409)
        self.assertEqual(self.fake.count, 1)
        self.assertEqual((await self.x.get("/v1/jobs/same", headers={"x-project-id":"other"})).status_code, 403)

    async def test_untrusted_context_denied_before_mcp(self):
        data = self.payload("forged")
        for headers in ({"x-organization-id": "other"}, {"x-project-write": "false"}):
            self.assertEqual((await self.x.post("/v1/calls", json=data, headers=headers)).status_code, 403)
        self.assertEqual((await self.x.post("/v1/calls", json=data | {"principal": {"admin": True}})).status_code, 422)
        self.assertEqual(self.fake.total, 0)

    async def test_cross_entry_unknown_hold_pause_no_auto_resubmit(self):
        self.fake.mode = "unknown"
        for i, action in enumerate(("hold", "pause")):
            await self.config(on_submission_unknown=action)
            key = "unknown-"+action
            call = await self.submit(key, "/v1/jobs")
            self.assertEqual((call["accepted"], call["state"]), (None, "submission_unknown"))
            self.assertEqual(self.q.status()["status"], "active" if action == "hold" else "paused")
            await self.g.tick()
            self.assertEqual((await self.submit(key))["call_id"], call["call_id"])
            self.assertEqual(self.fake.count, i+1)

    async def reconcile(self, action):
        self.fake.mode = "unknown"
        call = await self.submit("unknown")
        data = {"action": action, "revision": 0}
        if action == "bind":
            data["media_id"] = "asset"
        url = "/calls/"+call["call_id"]+"/reconcile"
        self.assertEqual((await self.m.post(url, json=data)).status_code, 200)
        self.assertEqual((await self.m.post(url, json=data)).status_code, 409)
        self.fake.mode = "success"
        await self.g.tick()
        await self.g.tick()
        self.assertEqual(self.fake.count, 2 if action == "retry" else 1)
        self.assertEqual(self.fake.reads, int(action == "bind"))
        self.assertEqual(self.q.usage()["reserved"], 0)
        self.assertEqual(self.q.usage()["credits"], 0 if action == "fail" else 3)
        self.assertEqual(self.q.find("key","unknown")["state"], "failed" if action == "fail" else "succeeded")

    async def test_reconcile_bind_existing(self):
        await self.reconcile("bind")

    async def test_reconcile_fail_release_once(self):
        await self.reconcile("fail")

    async def test_reconcile_retry_once(self):
        await self.reconcile("retry")

    async def test_restart_unknown_never_resubmitted(self):
        data = self.payload("interrupted")
        key, payload = self.g.normalize(data)
        row = self.q.intent("owner", key, payload)
        self.q.admit(row["id"], 3, 100)
        self.q.db.close()
        self.q = Queue(self.root / "q.sqlite", self.clock)
        self.g.q = self.q
        await self.g.tick()
        self.assertEqual(self.q.find("key", key)["state"], "submission_unknown")
        self.assertEqual(self.q.usage()["reserved"], 3)
        self.assertEqual(self.fake.count, 0)

    async def test_media_valid_spoof_digest_corrupt_path(self):
        raw = self.fake.path.read_bytes()
        async def upload(data, mime, digest=None):
            return await self.x.put("/v1/inputs", content=data, headers={"content-type": mime,
                            "x-content-sha256": digest or hashlib.sha256(data).hexdigest()})
        self.assertEqual((await upload(raw,"image/png")).status_code, 200)
        self.assertEqual((await upload(raw,"image/jpeg")).status_code, 422)
        self.assertEqual((await upload(raw,"image/png","0"*64)).status_code, 422)
        self.assertEqual((await upload(raw[:24],"image/png")).status_code, 422)
        data = self.payload("path")
        data["parameters"]["reference_images"] = ["../any.png"]
        self.assertEqual((await self.x.post("/v1/calls", json=data)).status_code, 422)
        self.assertEqual(self.fake.count, 0)
        call = await self.submit("valid")
        response = await self.x.get("/v1/calls/"+call["call_id"]+"/result")
        self.assertEqual(response.content, raw)
        self.assertEqual(response.headers["x-content-sha256"], hashlib.sha256(raw).hexdigest())

    async def test_dashboard_bundle_and_five_tabs(self):
        build(Path("dashboard"), self.root/"bundle", __version__)
        assets = DashboardAssets(self.root/"bundle")
        modules = [assets.asset(k)[0].decode() for k in assets.files if k.endswith(".mjs")]
        for label in ("账号", "调用记录", "审计日志", "模型与价目", "策略", "授权"):
            self.assertIn(label, "".join(modules))
        self.assertIn("sdk.app.request", "".join(modules))
        self.assertEqual(json.loads(assets.declaration)["appId"], "google-flow")

    async def test_missing_upstream_charge_preserves_result_and_reservation(self):
        self.fake.price = None
        result = await self.submit("missing-charge")
        self.assertEqual((result["state"], result["credits"], result["reserved"]), ("succeeded", None, 3))
        self.assertEqual((await self.x.get("/v1/jobs/missing-charge/content")).content, self.fake.path.read_bytes())
        self.assertEqual(self.fake.count, 1)

    async def test_valid_video_content_and_cache_integrity(self):
        target = self.root / "fixture.mp4"
        subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "color=s=16x16:r=1",
                        "-frames:v", "2", "-c:v", "mpeg4", "-movflags", "+faststart", str(target)], check=True)
        raw = target.read_bytes()
        meta = self.g.media.put(raw, "video/mp4")
        self.assertEqual((meta["width"],meta["height"],meta["duration"]), (16,16,2))
        self.assertEqual(self.g.media.read(meta["sha256"]), raw)
        with self.assertRaises(Error):
            self.g.media.put(raw[:30], "video/mp4")
        (self.g.media.root / meta["sha256"]).write_bytes(b"changed")
        with self.assertRaises(Error):
            self.g.media.read(meta["sha256"])

    async def artifact_response_loss(self, stage):
        from artifact_fixture import check_response_loss
        subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "color=s=16x16:r=1",
                        "-frames:v", "2", "-c:v", "mpeg4", "-movflags", "+faststart",
                        str(self.root / "fixture.mp4")], check=True)
        self.g.client.prices["veo-fast"] = 3
        data = self.payload("video-task", "/v1/jobs") | {"model":"veo-fast", "capability":"video"}
        response = await self.x.post("/v1/jobs", json=data)
        self.assertEqual(response.json()["state"], "succeeded")
        raw = await self.x.get("/v1/jobs/video-task/content")
        self.assertEqual(raw.status_code, 200)
        path = self.root / "consumer.mp4"
        path.write_bytes(raw.content)
        await asyncio.to_thread(check_response_loss, self, self.root, stage, path)
        self.assertEqual(self.fake.count, 1)

    async def test_video_artifact_prepare_response_lost(self):
        await self.artifact_response_loss("prepare")

    async def test_video_artifact_put_response_lost(self):
        await self.artifact_response_loss("put")

    async def test_video_artifact_commit_response_lost(self):
        await self.artifact_response_loss("commit")

    async def test_veo_quality_rejects_reference_images_at_gateway(self):
        data = self.payload("vq-ref", "/v1/jobs") | {
            "model": "veo-quality",
            "capability": "video",
            "parameters": {"prompt": "dance", "aspect": "9:16", "duration": 8, "reference_images": ["a" * 64]},
        }
        response = await self.x.post("/v1/jobs", json=data)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json(), {"code": "invalid_field", "field": "reference_images"})
        self.assertEqual(self.fake.count, 0)

    async def test_upscale_succeeded_and_fallback_original(self):
        for mode, expected_status in (
            ("success", "upscaled"),
            ("upscale_error", "fallback_original"),
            ("upscale_missing", "fallback_original"),
        ):
            with self.subTest(mode=mode):
                self.fake.mode = mode
                key = f"up-{mode}"
                data = self.payload(key, "/v1/calls")
                data["parameters"]["upscale"] = "2k"
                res = await self.x.post("/v1/calls", json=data)
                self.assertEqual(res.status_code, 200, res.text)
                body = res.json()
                self.assertEqual(body["state"], "succeeded")
                self.assertEqual(body["upscale_status"], expected_status)
                self.assertEqual(body["upscale_target"], "2k")

    async def test_video_edit_request_check_and_pre_send_refusal_vs_unknown(self):
        from google_flow_gateway.video_edit import _video_edit_request_check, classify_video_edit_exception
        from gflow_cli.api.transports._mc_common import ABORTED_BEFORE_SEND
        from gflow_cli.errors import GFlowError, WireFormatError

        vid_id = "vid-11111111"
        img_id = "img-22222222"
        check = _video_edit_request_check(vid_id, (img_id,))

        self.assertIsNone(check("other_rpc", ""))
        self.assertIsInstance(check("jIps6", ""), WireFormatError)
        self.assertIsInstance(check("jIps6", f"{vid_id} {img_id}"), WireFormatError)
        self.assertIsInstance(check("jIps6", f"macaron_cookie_edit {vid_id}"), WireFormatError)
        self.assertIsInstance(check("jIps6", f"macaron_cookie_edit {img_id}"), WireFormatError)
        self.assertIsNone(check("jIps6", f"macaron_cookie_edit {vid_id} {img_id}"))

        refused_exc = WireFormatError(detail="submit_refused_before_send", route="batchexecute:jIps6")
        refused_exc.charged = "no"
        refused_exc.remediation_hint = ABORTED_BEFORE_SEND
        refused_res = classify_video_edit_exception(refused_exc, entered_submit=True)
        self.assertEqual(refused_res["error"]["charged"], "no")
        self.assertIs(refused_res["error"]["accepted"], False)

        pre_click_res = classify_video_edit_exception(RuntimeError("video_reference_attach_timeout"), entered_submit=False)
        self.assertEqual(pre_click_res["error"]["charged"], "no")
        self.assertIs(pre_click_res["error"]["accepted"], False)

        post_click_exc = GFlowError(detail="timed out waiting for workflow media id", charged="maybe")
        unknown_res = classify_video_edit_exception(post_click_exc, entered_submit=True)
        self.assertEqual(unknown_res["error"]["charged"], "maybe")
        self.assertIsNone(unknown_res["error"]["accepted"])

        # Dispatch integration: pre-send refusal -> failed/rejected (reserve=0), post-click timeout -> submission_unknown (reserve=10)
        target = self.root / "fixture.mp4"
        subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "color=s=16x16:r=1",
                        "-frames:v", "2", "-c:v", "mpeg4", "-movflags", "+faststart", str(target)], check=True)
        vraw = target.read_bytes()
        vdigest = hashlib.sha256(vraw).hexdigest()
        iraw = self.fake.path.read_bytes()
        idigest = hashlib.sha256(iraw).hexdigest()
        self.assertEqual((await self.x.put("/v1/inputs", content=vraw, headers={"content-type": "video/mp4", "x-content-sha256": vdigest})).status_code, 200)
        self.assertEqual((await self.x.put("/v1/inputs", content=iraw, headers={"content-type": "image/png", "x-content-sha256": idigest})).status_code, 200)
        self.g.client.prices["omni-1.2-flash"] = 10

        orig_catalog = self.g.client.catalog
        async def _fake_catalog():
            cat = await orig_catalog()
            cat["models"]["video"] = sorted(set(cat["models"]["video"]) | {"omni-1.2-flash"})
            return cat
        self.g.client.catalog = _fake_catalog

        async def _mock_pre_send(**kwargs):
            return refused_res
        self.g.client.generate_video_edit = _mock_pre_send
        job1 = {
            "request_id": "edit-refused",
            "model": "omni-1.2-flash",
            "capability": "video",
            "parameters": {"prompt": "swap", "aspect": "16:9", "duration": 3, "reference_images": [idigest], "reference_videos": [vdigest]},
        }
        r1 = (await self.x.post("/v1/jobs", json=job1)).json()
        self.assertEqual((r1["state"], r1["accepted"], r1["reason_code"], r1["reserved"]), ("failed", False, "rejected", 0))

        async def _mock_unknown(**kwargs):
            return unknown_res
        self.g.client.generate_video_edit = _mock_unknown
        job2 = dict(job1, request_id="edit-unknown")
        r2 = (await self.x.post("/v1/jobs", json=job2)).json()
        self.assertEqual((r2["state"], r2["accepted"], r2["reason_code"], r2["reserved"]), ("submission_unknown", None, "acceptance_unknown", 10))
