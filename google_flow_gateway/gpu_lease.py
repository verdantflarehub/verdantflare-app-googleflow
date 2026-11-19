"""Persistent recovery boundary shared by the scheduler and management listener."""
import asyncio
import time

from .policy import Error


class Lease:
    def __init__(self, queue, scheduler, identity, gpu_uuid, stop_and_settle,
                 timeout_minutes=30, sleep=asyncio.sleep, monotonic=time.monotonic):
        if type(timeout_minutes) is not int or timeout_minutes <= 0:
            raise Error("invalid_field", field="lease_reconcile_timeout_minutes")
        self.q, self.scheduler, self.identity = queue, scheduler, identity
        self.gpu_uuid, self.settle = gpu_uuid, stop_and_settle
        self.timeout, self.sleep, self.monotonic = timeout_minutes * 60, sleep, monotonic
        self.valid = False
        self.failures = self.sequence = 0

    def record(self):
        row = self.q.db.execute("SELECT value FROM settings WHERE key='lease'").fetchone()
        return self.q.setting("lease") if row else None

    async def recover(self):
        self.valid = False
        with self.q.transaction():
            if not self.q.setting("pause"):
                self.q.pause("gpu_lease_lost")
        old = self.record()
        deadline = self.monotonic() + self.timeout
        if old:
            while await self.scheduler.state(old) not in ("released", "expired", "reclaimed"):
                if self.monotonic() >= deadline:
                    with self.q.transaction():
                        self.q.audit("system", "lease_reconcile_timeout", {})
                    return False
                await self.sleep(min(5, deadline - self.monotonic()))
        started = self.monotonic()
        grant = await self.scheduler.acquire()
        if not grant:
            return False
        with self.q.transaction():
            self.q.save("lease", grant)
        bound = await self.scheduler.bind(grant, self.identity)
        if not bound or self.monotonic() - started >= 60 or grant.get("gpu_uuid") != self.gpu_uuid:
            await self.scheduler.release(grant)
            if grant.get("gpu_uuid") != self.gpu_uuid:
                raise SystemExit(78)
            return False
        self.valid = True
        self.sequence = self.failures = 0
        with self.q.transaction():
            if self.q.setting("pause").get("reason") == "gpu_lease_lost":
                self.q.resume("system", True)
        return True

    async def heartbeat(self):
        self.sequence += 1
        try:
            state = await self.scheduler.heartbeat(self.record(), self.sequence)
        except (OSError, TimeoutError):
            self.failures += 1
            if self.failures < 2:
                return
            state = "lost"
        if state in ("active", "leased", "running"):
            self.failures = 0
            return
        self.valid = False
        await self.settle()
        with self.q.transaction():
            self.q.pause("gpu_lease_lost")
            self.q.db.execute("""UPDATE calls SET state='submission_unknown',accepted=NULL,
                reason='acceptance_unknown' WHERE state IN ('dispatching','running')""")

    async def close(self):
        self.valid = False
        await self.settle()
        if self.record():
            await self.scheduler.release(self.record())
