"""Public tools on the loopback MCP service. Never expose raw upstream errors."""
import asyncio
import json
import time
from contextlib import asynccontextmanager
import httpx2
from mcp.client.streamable_http import streamable_http_client
from .policy import Error

MODELS = {"image": {"nano2", "nano21", "nano-pro"}, "video": {"veo-lite", "veo-fast", "veo-quality", "omni-1.2-flash"}}
VIDEO_MODEL_LIMITS = {
    "veo-lite": {"max_refs": 3, "durations": (4, 6, 8)},
    "veo-fast": {"max_refs": 3, "durations": (4, 6, 8)},
    "veo-quality": {"max_refs": 0, "durations": (4, 6, 8)},
    "omni-1.2-flash": {"max_refs": 7, "durations": (3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15)},
}
UNAUTHENTICATED_STATUSES = {"unauthenticated", "not_authenticated", "expired", "signed_out", "logged_out"}


class GFlow:
    def __init__(self, session, prices=None):
        self.session = session
        self.prices = prices or {}
        self._account_lock = asyncio.Lock()
        self._account_cache = None
        self._account_cached_at = 0.0
        self._account_refresh_task = None

    async def tool(self, name, **arguments):
        try:
            async with self.session() as session:
                reply = await session.call_tool(name, arguments)
                value = reply.structured_content
                if value is None:
                    value = json.loads(reply.content[0].text)
                if not isinstance(value, dict):
                    raise ValueError()
                return value
        except Exception:
            raise Error("channel_unavailable", 503) from None

    async def account(self):
        async with self._account_lock:
            auth = await self.tool("gflow_auth_status")
            status = auth.get("status")
            if status in ("throttled", "error", "unavailable", "profile_locked"):
                raise Error("channel_unavailable", 503)
            if status in UNAUTHENTICATED_STATUSES:
                value = {"authorized": False, "credits": None, "email": auth.get("user_email"), "tier": None}
                self._account_cache = value
                self._account_cached_at = time.monotonic()
                return dict(value)
            if status != "authenticated":
                raise Error("channel_unavailable", 503)
            result = await self.tool("gflow_get_credits")
            balance = result.get("credits")
            if type(balance) is not int or balance < 0:
                raise Error("balance_unavailable", 503)
            value = {
                "authorized": True,
                "credits": balance,
                "email": result.get("email") or auth.get("user_email"),
                "tier": result.get("service_tier") or result.get("user_paygate_tier") or result.get("sku"),
            }
            self._account_cache = value
            self._account_cached_at = time.monotonic()
            return dict(value)

    def trigger_account_refresh(self):
        if self._account_refresh_task is None or self._account_refresh_task.done():
            async def _run():
                try:
                    await self.account()
                except Exception:
                    pass
            self._account_refresh_task = asyncio.create_task(_run())
        return self._account_refresh_task

    async def cached_account(self, ttl=60.0, max_wait=1.5):
        now = time.monotonic()
        if self._account_cache is not None:
            if now - self._account_cached_at >= ttl:
                self.trigger_account_refresh()
            return dict(self._account_cache)
        task = self.trigger_account_refresh()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=max_wait)
        except asyncio.TimeoutError:
            pass
        if self._account_cache is not None:
            return dict(self._account_cache)
        return {"authorized": None, "credits": None}

    async def catalog(self):
        raw = await self.tool("gflow_list_models")
        tools = await self.tool("gflow_list_tools")
        catalog = {}
        for kind, allowed in MODELS.items():
            catalog[kind] = sorted({alias for item in raw[kind]["models"]
                                    for alias in item["aliases"] if alias in allowed})
        return {"models": catalog, "tools": [item["name"] for item in tools["tools"]]}

    def quote(self, payload):
        amount = self.prices.get(payload["model"], payload.get("budget_limit"))
        if type(amount) is not int or amount < 0:
            raise Error("cost_unknown", 409)
        return amount


def local_session(token, port=18000, timeout=600.0):
    @asynccontextmanager
    async def session():
        async with httpx2.AsyncClient(headers={"Authorization": "Bearer " + token},
                                      trust_env=False, follow_redirects=False,
                                      timeout=httpx2.Timeout(timeout, connect=10.0)) as http:
            async with streamable_http_client(f"http://127.0.0.1:{port}/mcp", http_client=http) as streams:
                from mcp.client import ClientSession
                async with ClientSession(*streams) as client:
                    await client.initialize()
                    yield client
    return session
