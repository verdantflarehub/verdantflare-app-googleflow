import asyncio
import json
import mimetypes
import os
from pathlib import Path
from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Route

from .policy import DEFAULTS, Error
from .queue import DEFAULT_ACCOUNT_ID
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


async def _account_snapshot(g, account_id, force_refresh=False):
    try:
        client = g.client_for(account_id)
        if not force_refresh and hasattr(client, "cached_account"):
            return await client.cached_account()
        return await client.account()
    except Error:
        return {"authorized": None, "credits": None}


def management(g, token, station, assets=None, lease=None, desktop=None, proxies=None):
    async def _build_accounts_payload():
        accounts_list = g.q.list_accounts()
        snapshots = await asyncio.gather(*(_account_snapshot(g, acct["id"]) for acct in accounts_list))
        items = []
        total_credits = 0
        total_reserved = 0
        active_accounts = 0
        for acct, live in zip(accounts_list, snapshots):
            aid = acct["id"]
            usage = g.q.usage(aid)
            if isinstance(live.get("credits"), int):
                total_credits += live["credits"]
            total_reserved += usage["reserved"]
            if acct["enabled"] and acct["status"] == "active" and live.get("authorized"):
                active_accounts += 1
            items.append(acct | {"account": live, "usage": usage})
        return {
            "accounts": items,
            "summary": {
                "total_accounts": len(items),
                "active_accounts": active_accounts,
                "max_accounts": g.q.max_accounts,
                "total_credits": total_credits,
                "total_reserved": total_reserved,
            },
        }

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

        # --- System network proxies endpoint ---
        if path == "/proxies":
            if not proxies:
                return reply({"schema_version": 1, "items": [], "configured": False})
            res = proxies.list(g.org)
            return reply(res | {"configured": True})

        # --- Multi-account management endpoints ---
        if path == "/accounts":
            if request.method == "POST":
                body_data = await object_body(request)
                if "proxy_ref" in body_data:
                    if not proxies:
                        raise Error("proxy_unavailable", 502)
                    resolved_url, resolved_name = proxies.resolve_url(g.org, body_data["proxy_ref"])
                    body_data = dict(body_data)
                    body_data["proxy_url"] = resolved_url
                    body_data["proxy_name"] = resolved_name
                    ref = body_data["proxy_ref"] or {}
                    body_data["proxy_id"] = ref.get("proxy_id", "") if ref.get("mode") == "proxy" else ""
                    body_data["proxy_revision"] = int(ref.get("revision", 0)) if ref.get("mode") == "proxy" else 0
                acct = q.create_account(body_data, actor)
                if g.slots is not None and not isinstance(g.slots, dict):
                    await g.slots.sync_all()
                return reply(acct, status=201)
            return reply(await _build_accounts_payload())

        if path.startswith("/accounts/"):
            aid = request.path_params["account_id"]
            if path == f"/accounts/{aid}":
                if request.method == "PUT":
                    body_data = await object_body(request)
                    if "proxy_ref" in body_data:
                        if not proxies:
                            raise Error("proxy_unavailable", 502)
                        resolved_url, resolved_name = proxies.resolve_url(g.org, body_data["proxy_ref"])
                        body_data = dict(body_data)
                        body_data["proxy_url"] = resolved_url
                        body_data["proxy_name"] = resolved_name
                        ref = body_data["proxy_ref"] or {}
                        body_data["proxy_id"] = ref.get("proxy_id", "") if ref.get("mode") == "proxy" else ""
                        body_data["proxy_revision"] = int(ref.get("revision", 0)) if ref.get("mode") == "proxy" else 0
                    acct, proxy_changed = q.update_account(aid, body_data, actor)
                    if g.slots is not None and not isinstance(g.slots, dict):
                        if proxy_changed:
                            await g.slots.restart_slot(aid)
                        else:
                            await g.slots.sync_all()
                    return reply(acct)
                if request.method == "DELETE":
                    res = q.delete_account(aid, actor)
                    if g.slots is not None and not isinstance(g.slots, dict):
                        await g.slots.sync_all()
                        g.slots.cleanup_deleted_dirs(aid)
                    return reply(res)
                acct = q.get_account(aid)
                return reply(acct | {"account": await _account_snapshot(g, aid), "usage": q.usage(aid)})

            if path == f"/accounts/{aid}/policy":
                if request.method == "PUT":
                    q.configure(await object_body(request), actor, account_id=aid)
                return reply(q.account_policy(aid))

            if path == f"/accounts/{aid}/policy/defaults":
                q.configure(DEFAULTS, actor, account_id=aid)
                return reply(q.account_policy(aid))

            if path.startswith(f"/accounts/{aid}/action/"):
                act = request.path_params["action"]
                if act == "refresh":
                    live = await _account_snapshot(g, aid, force_refresh=True)
                    with q.transaction():
                        q.audit(actor, "balance_refresh", {"account_id": aid, "credits": live.get("credits")})
                    return reply(live)
                q.action(act, actor, True if lease is None else bool(lease.valid), account_id=aid)
                return reply({"ok": True})

            if path == f"/accounts/{aid}/desktop/login":
                if not desktop:
                    raise Error("desktop_unavailable", 503)
                if lease is not None and not lease.valid:
                    raise Error("gpu_lease_lost", 409)
                acct = q.get_account(aid)
                result = await desktop("open", account_id=aid)
                with q.transaction():
                    q.audit(actor, "desktop_login", {"account_id": aid, "proxy_url": acct["proxy_url"]})
                return reply(result | {"account_id": aid})

            if path == f"/accounts/{aid}/desktop/check-auth":
                return reply(await _account_snapshot(g, aid, force_refresh=True))

        # --- Legacy single-account compatible endpoints (default / aggregate) ---
        if path == "/overview":
            account = await _account_snapshot(g, DEFAULT_ACCOUNT_ID)
            desktop_state = {"open": False, "available": False}
            if desktop:
                try:
                    desktop_state = await desktop("status")
                except Error:
                    desktop_state = {"open": False, "available": False}
            accounts_payload = await _build_accounts_payload()
            with q.transaction():
                status = q.status(DEFAULT_ACCOUNT_ID)
                timed_out = bool(status.get("reason") == "gpu_lease_lost" and q.db.execute(
                    "SELECT 1 FROM audit WHERE action='lease_reconcile_timeout' AND time>=? LIMIT 1",
                    (status["since"],)).fetchone())
                return reply(status | {
                    "account": account,
                    "accounts": accounts_payload["accounts"],
                    "summary": accounts_payload["summary"],
                    "today": q.usage(DEFAULT_ACCOUNT_ID),
                    "policy": q.account_policy(DEFAULT_ACCOUNT_ID),
                    "desktop": desktop_state,
                    "lease_valid": True if lease is None else bool(lease.valid),
                    "no_gpu": lease is None,
                    "lease_reconcile_timeout": timed_out,
                })
        if path == "/policy":
            if request.method == "PUT":
                q.configure(await object_body(request), actor, account_id=DEFAULT_ACCOUNT_ID)
            return reply(q.account_policy(DEFAULT_ACCOUNT_ID))
        if path == "/policy/defaults":
            q.configure(DEFAULTS, actor, account_id=DEFAULT_ACCOUNT_ID)
            return reply(q.account_policy(DEFAULT_ACCOUNT_ID))
        if path.startswith("/channel/"):
            q.action(request.path_params["action"], actor, True if lease is None else bool(lease.valid),
                     account_id=DEFAULT_ACCOUNT_ID)
            return reply({"ok": True})
        if path == "/calls":
            filter_acct = request.query_params.get("account_id")
            labels = {a["id"]: a["label"] for a in q.list_accounts()}
            if filter_acct:
                rows = q.db.execute("SELECT * FROM calls WHERE account_id=? ORDER BY rowid DESC LIMIT 200", (filter_acct,))
            else:
                rows = q.db.execute("SELECT * FROM calls ORDER BY rowid DESC LIMIT 200")
            return reply([view(row, account_label=labels.get(row["account_id"], row["account_id"])) for row in rows])
        if path == "/audit":
            filter_acct = request.query_params.get("account_id")
            out = []
            for row in q.db.execute("SELECT * FROM audit ORDER BY id DESC LIMIT 200"):
                detail = json.loads(row["detail"])
                if filter_acct and detail.get("account_id") != filter_acct:
                    continue
                out.append(dict(row) | {"detail": detail, "account_id": detail.get("account_id")})
            return reply(out)
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
            return reply(await _account_snapshot(g, DEFAULT_ACCOUNT_ID, force_refresh=True))
        if not desktop:
            if action == "status":
                return reply({"open": False, "available": False})
            raise Error("desktop_unavailable", 503)
        if action in ("open", "turn", "login") and lease is not None and not lease.valid:
            raise Error("gpu_lease_lost", 409)
        session_id = None
        if action == "close" and request.method == "POST":
            try:
                close_body = await object_body(request)
                if isinstance(close_body.get("session_id"), int):
                    session_id = close_body["session_id"]
            except Error:
                pass
        result = await desktop(action, session_id=session_id) if action == "close" else await desktop(action)
        if request.method != "GET":
            with q.transaction():
                q.audit(actor, "desktop_" + action, {"account_id": DEFAULT_ACCOUNT_ID})
        return reply(result)

    routes = [Route(path, handle) for path in ("/overview", "/proxies", "/calls", "/audit", "/desktop",
              "/desktop/settings", "/desktop/assets/{asset:path}",
              "/internal/dashboard-declaration", "/internal/dashboard-index", "/internal/dashboard/{asset:path}")]
    routes.append(Route("/accounts", handle, methods=["GET", "POST"]))
    routes.append(Route("/accounts/{account_id}", handle, methods=["GET", "PUT", "DELETE"]))
    routes.append(Route("/accounts/{account_id}/policy", handle, methods=["GET", "PUT"]))
    routes.append(Route("/accounts/{account_id}/policy/defaults", handle, methods=["POST"]))
    routes.append(Route("/accounts/{account_id}/action/{action}", handle, methods=["POST"]))
    routes.append(Route("/accounts/{account_id}/desktop/login", handle, methods=["POST"]))
    routes.append(Route("/accounts/{account_id}/desktop/check-auth", handle, methods=["POST"]))
    routes.append(Route("/policy", handle, methods=["PUT", "GET"]))
    routes += [Route(path, handle, methods=["POST"]) for path in ("/policy/defaults", "/channel/{action}",
               "/calls/{value}/reconcile", "/desktop/{action}")]
    return Starlette(routes=routes, exception_handlers={Error: handle_error})
