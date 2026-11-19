from uuid import uuid4
import httpx
from .policy import Error


class Scheduler:
    def __init__(self, url, token, gpu, peak):
        self.http = httpx.AsyncClient(base_url=url.rstrip("/") + "/internal/v1/gpu-scheduler/leases/",
                                      headers={"Authorization": "Bearer " + token}, trust_env=False, timeout=10)
        self.gpu, self.peak, self.attempt = gpu, peak, uuid4().hex
        self.version = 0

    async def acquire(self):
        data = dict(idempotency_key="acq:" + self.attempt, holder_app_id="google-flow",
                    consumer_kind="google_flow_desktop", task_id="google-flow-desktop", attempt_id=self.attempt,
                    stage_id="desktop", ttl_seconds=90, candidate_gpu_uuids=[self.gpu], peak_budget_mib=self.peak)
        result = (await self.http.post("acquire", json=data)).raise_for_status().json()
        if result.get("state") != "leased":
            return None
        self.version = result["version"]
        return {key: result[key] for key in ("lease_id", "gpu_uuid", "fencing_token", "version")}

    async def state(self, grant):
        response = await self.http.get(grant["lease_id"])
        return response.raise_for_status().json()["state"]

    async def update(self, grant, action, suffix, **data):
        lid, fence = grant["lease_id"], grant["fencing_token"]
        result = await self.http.post(f"{lid}/{action}", json=dict(
            idempotency_key=f"{action}:{lid}:{suffix}", fencing_token=fence,
            expected_version=max(self.version, grant["version"]), **data))
        result.raise_for_status()
        latest = result.json()
        if latest.get("fencing_token") != fence:
            return "fence_mismatch"
        self.version = latest["version"]
        return latest["state"]

    async def bind(self, grant, identity):
        return await self.update(grant, "bind-target", identity["pod_uid"], **identity,
                                 actual_gpu_uuids=[self.gpu], ttl_seconds=90) in ("leased", "running")

    async def heartbeat(self, grant, sequence):
        try:
            return await self.update(grant, "heartbeat", sequence, ttl_seconds=90)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (404, 409, 410):
                return "lost"
            raise OSError("scheduler_unavailable") from None
        except httpx.HTTPError:
            raise OSError("scheduler_unavailable") from None

    async def release(self, grant):
        await self.update(grant, "release", "settled", worker_settled=True)
        self.attempt = uuid4().hex
