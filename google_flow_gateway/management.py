import json
import mimetypes
import os
from pathlib import Path
from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Route

from .policy import DEFAULTS, Error
from .server import ID, bearer, handle_error, header, object_body, reply, view

_WEBCLIENT_SUFFIXES = {".html", ".js", ".css", ".svg", ".png", ".ico", ".woff", ".woff2", ".ttf", ".json"}
_WEBCLIENT_MAX_BYTES = 4 * 1024 * 1024
_DEFAULT_DESKTOP_SETTINGS = {
    "BDWIND_FRAMERATE": 30,
    "BDWIND_VIDEO_BITRATE": 4000,
    "BDWIND_AUDIO_BITRATE": 64000,
    "BDWIND_ENCODER": "nvh264enc",
    "BDWIND_RESOLUTION": "1280x720",
}


def management(g, token, station, assets=None, lease=None, desktop=None):
    async def handle(request):
        bearer(request, token)
        path = request.url.path
        if path.startswith("/internal/dashboard"):
            if not assets:
                raise Error("dashboard_unavailable", 503)
            if path == "/internal/dashboard-declaration":
                return Response(assets.declaration, media_type="application/json")
            if path == "/internal/dashboard-index":
                return Response(assets.index, media_type="application/json")
            try:
                data, mime = assets.asset(request.path_params["asset"])
            except (KeyError, ValueError):
                raise Error("not_found", 404) from None
            return Response(data, media_type=mime)
        actor = header(request, "x-user-id")
        if header(request, "x-organization-id") != g.org or header(request, "x-station-id") != station:
            raise Error("forbidden", 403)
        q = g.q
        if path == "/overview":
            try:
                account = await g.client.account()
            except Error:
                account = {"authorized": None, "credits": None}
            with q.transaction():
                status = q.status()
                timed_out = bool(status.get("reason") == "gpu_lease_lost" and q.db.execute(
                    "SELECT 1 FROM audit WHERE action='lease_reconcile_timeout' AND time>=? LIMIT 1",
                    (status["since"],)).fetchone())
                return reply(status | {"account": account, "today": q.usage(), "policy": q.setting("policy"),
                                      "lease_valid": True if lease is None else bool(lease.valid),
                                      "lease_reconcile_timeout": timed_out})
        if path == "/policy":
            if request.method == "PUT":
                q.configure(await object_body(request), actor)
            return reply(q.setting("policy"))
        if path == "/policy/defaults":
            q.configure(DEFAULTS, actor)
            return reply(q.setting("policy"))
        if path.startswith("/channel/"):
            q.action(request.path_params["action"], actor, True if lease is None else bool(lease.valid))
            return reply({"ok": True})
        if path == "/calls":
            return reply([view(row) for row in q.db.execute("SELECT * FROM calls ORDER BY rowid DESC LIMIT 200")])
        if path == "/audit":
            return reply([dict(row) | {"detail": json.loads(row["detail"])} for row in
                          q.db.execute("SELECT * FROM audit ORDER BY id DESC LIMIT 200")])
        if path.startswith("/calls/"):
            data = await object_body(request)
            media_id = data.get("media_id")
            if media_id is not None and (not isinstance(media_id, str) or not ID.fullmatch(media_id)):
                raise Error("invalid_field", field="media_id")
            if type(data.get("revision")) is not int:
                raise Error("invalid_field", field="revision")
            call_id = request.path_params["value"]
            q.reconcile(call_id, data["revision"], data.get("action"), actor, media_id)
            return reply(view(q.find("id", call_id)))
        if path == "/desktop/settings":
            return reply(dict(_DEFAULT_DESKTOP_SETTINGS))
        if path.startswith("/desktop/assets/"):
            root = Path(os.environ.get("GOOGLE_FLOW_WEB_ROOT", "/app/webclient")).resolve()
            rel = request.path_params.get("asset") or "index.html"
            target = (root / rel).resolve()
            if root not in target.parents or not target.is_file() or target.suffix not in _WEBCLIENT_SUFFIXES or target.stat().st_size > _WEBCLIENT_MAX_BYTES:
                raise Error("not_found", 404)
            mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            return Response(target.read_bytes(), media_type=mime, headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            })
        action = request.path_params.get("action", "status")
        if action == "check-auth":
            return reply(await g.client.account())
        if not desktop:
            if action == "status":
                return reply({"open": False, "available": False})
            raise Error("desktop_unavailable", 503)
        if action in ("open", "turn", "login") and lease is not None and not lease.valid:
            raise Error("gpu_lease_lost", 409)
        result = await desktop(action)
        if request.method != "GET":
            with q.transaction():
                q.audit(actor, "desktop_" + action, {})
        return reply(result)

    routes = [Route(path, handle) for path in ("/overview", "/calls", "/audit", "/desktop",
              "/desktop/settings", "/desktop/assets/{asset:path}",
              "/internal/dashboard-declaration", "/internal/dashboard-index", "/internal/dashboard/{asset:path}")]
    routes.append(Route("/policy", handle, methods=["PUT", "GET"]))
    routes += [Route(path, handle, methods=["POST"]) for path in ("/policy/defaults", "/channel/{action}",
               "/calls/{value}/reconcile", "/desktop/{action}")]
    return Starlette(routes=routes, exception_handlers={Error: handle_error})
