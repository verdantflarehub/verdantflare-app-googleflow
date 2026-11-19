import asyncio
import secrets
import socket
import unittest

from mcp.server import MCPServer
import uvicorn

from google_flow_gateway.gflow_client import GFlow, local_session
from google_flow_gateway.runtime import Listener


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_loopback_streamable_mcp_bearer_and_redaction(self):
        fake = MCPServer("offline")
        seen = []
        token = secrets.token_urlsafe(32)

        @fake.tool()
        def gflow_auth_status() -> dict:
            return {"status": "authenticated", "user_email": "private"}

        @fake.tool()
        def gflow_get_credits() -> dict:
            return {"credits": 14, "service_tier": "fixture"}

        target = fake.streamable_http_app(stateless_http=True, json_response=True)
        async def guarded(scope, receive, send):
            if scope["type"] == "http":
                seen.append(dict(scope["headers"]).get(b"authorization") == ("Bearer " + token).encode())
            await target(scope, receive, send)

        with socket.socket() as sock:
            sock.bind(("127.0.0.1",0))
            sock.setblocking(False)
            port = sock.getsockname()[1]
            server = Listener(uvicorn.Config(guarded, access_log=False, log_level="error", ws="none"))
            task = asyncio.create_task(server.serve(sockets=[sock]))
            try:
                async with asyncio.timeout(8):
                    while not server.started:
                        await asyncio.sleep(.02)
                    account = await GFlow(local_session(token, port)).account()
                self.assertEqual(account, {"authorized":True, "credits":14, "tier":"fixture"})
                self.assertTrue(seen)
                self.assertTrue(all(seen))
            finally:
                server.should_exit = True
                await task
