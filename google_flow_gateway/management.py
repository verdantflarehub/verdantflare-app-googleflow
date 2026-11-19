import json
from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Route

from .policy import DEFAULTS, Error
from .server import ID, bearer, handle_error, header, object_body, reply, view


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
                                      "lease_valid": bool(lease and lease.valid), "lease_reconcile_timeout": timed_out})
        if path == "/policy":
            if request.method == "PUT":
                q.configure(await object_body(request), actor)
            return reply(q.setting("policy"))
        if path == "/policy/defaults":
            q.configure(DEFAULTS, actor)
            return reply(q.setting("policy"))
        if path.startswith("/channel/"):
            q.action(request.path_params["action"], actor, bool(lease and lease.valid))
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
        action = request.path_params.get("action", "status")
        if action == "check-auth":
            return reply(await g.client.account())
        if not desktop:
            if action == "status":
                return reply({"open": False, "available": False})
            raise Error("desktop_unavailable", 503)
        if action in ("open", "turn", "login") and not (lease and lease.valid):
            raise Error("gpu_lease_lost", 409)
        result = await desktop(action)
        if request.method != "GET":
            with q.transaction():
                q.audit(actor, "desktop_" + action, {})
        return reply(result)

    routes = [Route(path, handle) for path in ("/overview", "/calls", "/audit", "/desktop",
              "/internal/dashboard-declaration", "/internal/dashboard-index", "/internal/dashboard/{asset:path}")]
    routes.append(Route("/policy", handle, methods=["PUT", "GET"]))
    routes += [Route(path, handle, methods=["POST"]) for path in ("/policy/defaults", "/channel/{action}",
               "/calls/{value}/reconcile", "/desktop/{action}")]
    return Starlette(routes=routes, exception_handlers={Error: handle_error})
