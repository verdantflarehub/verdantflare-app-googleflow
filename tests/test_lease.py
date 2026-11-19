import json
from pathlib import Path
import secrets
import unittest

import httpx

from google_flow_gateway.gpu_lease import Lease
from google_flow_gateway.management import management
from google_flow_gateway.queue import Queue
from google_flow_gateway.turn import credentials
from types import SimpleNamespace


class FakeScheduler:
    def __init__(self):
        self.events = []
        self.grant = {"lease_id": "old", "gpu_uuid": "card", "fencing_token": 1}
        self.bound = True
        self.state_now = "released"
        self.heartbeat_state = "leased"

    async def state(self, old):
        self.events.append("state")
        return self.state_now

    async def acquire(self):
        self.events.append("acquire")
        return self.grant

    async def bind(self, grant, identity):
        self.events.append("bind")
        return self.bound

    async def heartbeat(self, grant, seq):
        self.events.append(("heartbeat", seq))
        if self.heartbeat_state is None:
            raise TimeoutError()
        return self.heartbeat_state

    async def release(self, grant):
        self.events.append("release")


class LeaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = 0
        self.q = Queue(":memory:", lambda: self.now)
        self.addCleanup(self.q.db.close)
        self.scheduler = FakeScheduler()

        async def settle():
            self.scheduler.events.append("settle")

        async def sleep(seconds):
            self.now += seconds

        self.lease = Lease(self.q, self.scheduler, {"pod_uid": "pod", "container_id": "container",
                           "boot_id": "boot"}, "card", settle, sleep=sleep, monotonic=lambda: self.now)
        with self.q.transaction():
            self.q.pause("gpu_lease_lost")

    async def test_gpu_recovery_requires_grant_bind_same_card(self):
        self.scheduler.grant = None
        self.assertFalse(await self.lease.recover())
        self.assertEqual(self.q.status()["reason"], "gpu_lease_lost")
        self.scheduler.grant = {"gpu_uuid": "card"}
        self.scheduler.bound = False
        self.assertFalse(await self.lease.recover())
        self.assertEqual(self.q.status()["reason"], "gpu_lease_lost")
        self.scheduler.bound = True
        self.scheduler.grant = {"gpu_uuid": "other"}
        with self.assertRaises(SystemExit) as error:
            await self.lease.recover()
        self.assertEqual(error.exception.code, 78)
        self.assertFalse(self.lease.valid)
        self.assertEqual(self.q.status()["reason"], "gpu_lease_lost")
        self.scheduler.grant = {"gpu_uuid": "card"}
        self.assertTrue(await self.lease.recover())
        self.assertEqual(self.q.status()["status"], "active")
        actor, details = self.q.db.execute("SELECT actor,detail FROM audit WHERE action='resume'").fetchone()
        self.assertEqual((actor, json.loads(details)["old"]["reason"]), ("system", "gpu_lease_lost"))

    async def test_gpu_manual_resume_without_lease_field_error(self):
        token = secrets.token_hex(32)
        app = management(SimpleNamespace(q=self.q, org="org"), token, "station", lease=self.lease)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            res = await client.post("/channel/resume", headers={"authorization": "Bearer "+token, "x-user-id":"actor",
                                    "x-station-id":"station", "x-organization-id":"org"})
        self.assertEqual((res.status_code,res.json()["field"]), (422, "gpu_lease"))
        self.assertEqual(self.q.status()["reason"], "gpu_lease_lost")

    async def test_gpu_lease_loss_settles_before_pause_and_no_resubmit(self):
        await self.lease.recover()
        row = self.q.intent("owner","running",{})
        self.q.admit(row["id"],3,10)
        self.scheduler.heartbeat_state = None
        await self.lease.heartbeat()
        self.assertTrue(self.lease.valid)
        await self.lease.heartbeat()
        self.assertFalse(self.lease.valid)
        self.assertEqual(self.scheduler.events[-1],"settle")
        self.assertEqual(self.q.status()["reason"],"gpu_lease_lost")
        self.assertEqual(self.q.find("id",row["id"])["state"],"submission_unknown")

    async def test_gpu_old_lease_wait_timeout_defaults_and_override(self):
        self.assertEqual(self.lease.timeout, 1800)
        self.lease = Lease(self.q,self.scheduler,{},"card",self.lease.settle,1,self.lease.sleep,lambda:self.now)
        with self.q.transaction():
            self.q.save("lease", {"lease_id":"old"})
        self.scheduler.state_now = "reconciling"
        self.assertFalse(await self.lease.recover())
        self.assertEqual(self.now, 60)
        self.assertNotIn("acquire",self.scheduler.events)
        self.assertEqual(self.q.status()["reason"], "gpu_lease_lost")
        self.assertEqual(self.q.db.execute("SELECT action FROM audit ORDER BY id DESC LIMIT 1").fetchone()[0],
                         "lease_reconcile_timeout")

    async def test_turn_short_lived_relay(self):
        result = credentials(["turn:relay.invalid"],secrets.token_hex(32),lambda:1)
        self.assertEqual(result["iceTransportPolicy"],"relay")
        self.assertEqual(result["expires_at"],901)
        self.assertTrue(result["iceServers"][0]["username"].startswith("901:"))
