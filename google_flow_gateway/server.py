import hmac
import json
import re
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .gflow_client import IMAGE_UPSCALES, MODELS, VIDEO_MODEL_LIMITS
from .media import MAX_SIZE
from .policy import Error
from .queue import encode

ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def bearer(request, token):
    values = request.headers.getlist("authorization")
    if not token or len(values) != 1 or not hmac.compare_digest(values[0].encode(), ("Bearer " + token).encode()):
        raise Error("unauthorized", 401)


def header(request, name):
    values = request.headers.getlist(name)
    if len(values) != 1 or not ID.fullmatch(values[0]):
        raise Error("forbidden", 403)
    return values[0]


async def body(request, maximum=65536):
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > maximum:
            raise Error("request_too_large", 413)
        data.extend(chunk)
    return bytes(data)


async def object_body(request):
    try:
        value = json.loads(await body(request))
        if isinstance(value, dict):
            return value
    except (ValueError, UnicodeError):
        pass
    raise Error("invalid_request", 400)


def reply(data, status=200):
    return JSONResponse(data, status_code=status, headers={"Cache-Control": "no-store"})


async def handle_error(request, exc):
    return reply({"code": exc.code, "field": exc.field}, exc.status)


def view(row, result_meta=None, account_label=None):
    if row is None:
        raise Error("not_found", 404)
    accepted = None if row["accepted"] is None else bool(row["accepted"])
    payload = json.loads(row["payload"])
    account_id = row["account_id"] if "account_id" in row.keys() else "default"
    data = dict(call_id=row["id"], request_id=row["key"], accepted=accepted, state=row["state"],
                started=row["started"], model=payload.get("model"), capability=payload.get("capability"),
                status=row["state"], reason_code=row["reason"], revision=row["revision"],
                reserved=row["reserve"], credits=None if row["reason"] == "charge_unconfirmed" else row["spent"],
                account_id=account_id)
    if account_label is not None:
        data["account_label"] = account_label
    if isinstance(result_meta, dict) and result_meta.get("upscale_status"):
        data["upscale_status"] = result_meta["upscale_status"]
        data["upscale_target"] = result_meta["upscale_target"]
    return data


class Gateway:
    def __init__(self, queue, client, media, output, org, slots=None):
        self.q, self.client, self.media, self.output, self.org = queue, client, media, output, org
        self.slots = slots
        self.admitted = lambda: True
        self._inflight = set()
        self.q.db.executescript("""
            CREATE TABLE IF NOT EXISTS inputs(owner TEXT, digest TEXT, PRIMARY KEY(owner,digest));
            CREATE TABLE IF NOT EXISTS results(id TEXT PRIMARY KEY, metadata TEXT);
        """)

    def client_for(self, account_id="default"):
        if self.slots is not None:
            if isinstance(self.slots, dict):
                if account_id in self.slots:
                    return self.slots[account_id]
                raise Error("channel_unavailable", 503)
            return self.slots.client_for(account_id)
        return self.client

    def output_for(self, account_id="default"):
        if self.slots is not None and not isinstance(self.slots, dict):
            return self.slots.output_for(account_id)
        return self.output

    def _active_clients(self):
        if self.slots is None:
            return {"default": self.client}
        if isinstance(self.slots, dict):
            return dict(self.slots)
        return {aid: slot.client for aid, slot in self.slots.slots.items() if slot.alive}

    def any_client(self):
        clients = self._active_clients()
        if "default" in clients:
            return clients["default"]
        if clients:
            return next(iter(clients.values()))
        if self.client is not None:
            return self.client
        raise Error("channel_unavailable", 503)

    def owner(self, request):
        org = header(request, "x-organization-id")
        if org != self.org or request.headers.getlist("x-project-write") != ["true"]:
            raise Error("forbidden", 403)
        return encode([org, header(request, "x-project-id"), header(request, "x-user-id")])

    def normalize(self, data):
        if set(data) - {"capability", "model", "parameters", "budget_limit", "idempotency_key", "request_id", "channel_account_id"}:
            raise Error("invalid_field")
        key = data.get("idempotency_key", data.get("request_id"))
        if not isinstance(key, str) or not ID.fullmatch(key) or data.get("request_id", key) != key:
            raise Error("invalid_field", field="idempotency_key")
        channel_account_id = data.get("channel_account_id")
        if channel_account_id is not None:
            if not isinstance(channel_account_id, str) or not ID.fullmatch(channel_account_id):
                raise Error("invalid_field", field="channel_account_id")
        kind, model, args = data.get("capability"), data.get("model"), data.get("parameters")
        if not isinstance(kind, str) or not isinstance(model, str) or model not in MODELS.get(kind, ()):
            raise Error("capability_not_supported")
        if not isinstance(args, dict) or set(args) - {"prompt", "aspect", "reference_images", "reference_videos", "duration", "upscale"}:
            raise Error("invalid_field", field="parameters")
        args = dict(args)
        if not isinstance(args.get("prompt"), str) or not 1 <= len(args["prompt"].strip()) <= 5000:
            raise Error("invalid_field", field="prompt")
        if args.get("aspect", "16:9") not in (("16:9", "9:16", "1:1") if kind == "image" else ("16:9", "9:16")):
            raise Error("invalid_field", field="aspect")
        if "duration" in args:
            allowed_durations = VIDEO_MODEL_LIMITS.get(model, {}).get("durations", (4, 6, 8))
            if kind != "video" or args["duration"] not in allowed_durations:
                raise Error("invalid_field", field="duration")
        if "upscale" in args:
            if args["upscale"] is None:
                args.pop("upscale")
            else:
                allowed_upscales = VIDEO_MODEL_LIMITS.get(model, {}).get("upscales", ()) if kind == "video" else IMAGE_UPSCALES
                if not isinstance(args["upscale"], str) or args["upscale"] not in allowed_upscales:
                    raise Error("invalid_field", field="upscale")
        refs = args.get("reference_images", [])
        max_refs = VIDEO_MODEL_LIMITS[model]["max_refs"] if kind == "video" else 3
        if not isinstance(refs, list) or len(refs) > max_refs or any(not isinstance(i, str) or not re.fullmatch("[a-f0-9]{64}", i) for i in refs):
            raise Error("invalid_field", field="reference_images")
        vrefs = args.get("reference_videos", [])
        max_vrefs = VIDEO_MODEL_LIMITS[model].get("max_video_refs", 0) if kind == "video" else 0
        if not isinstance(vrefs, list) or len(vrefs) > max_vrefs or any(not isinstance(i, str) or not re.fullmatch("[a-f0-9]{64}", i) for i in vrefs):
            raise Error("invalid_field", field="reference_videos")
        limit = data.get("budget_limit")
        if limit is not None and (type(limit) is not int or not 0 <= limit <= 2**31-1):
            raise Error("invalid_field", field="budget_limit")
        norm = dict(capability=kind, model=model, parameters=args, budget_limit=limit)
        if channel_account_id is not None:
            norm["channel_account_id"] = channel_account_id
        return key, norm

    def references(self, owner, payload):
        refs = payload["parameters"].get("reference_images", [])
        paths = []
        for ref in refs:
            if not self.q.db.execute("SELECT 1 FROM inputs WHERE owner=? AND digest=?", (owner, ref)).fetchone():
                raise Error("forbidden", 403)
            paths.append(str(self.media.path_with_ext(ref)))
        return paths

    def video_references(self, owner, payload):
        refs = payload["parameters"].get("reference_videos", [])
        paths = []
        for ref in refs:
            if not self.q.db.execute("SELECT 1 FROM inputs WHERE owner=? AND digest=?", (owner, ref)).fetchone():
                raise Error("forbidden", 403)
            paths.append(str(self.media.path_with_ext(ref)))
        return paths

    async def dispatch(self, row):
        if row["state"] != "pending" or row["id"] in self._inflight:
            return
        self._inflight.add(row["id"])
        try:
            if not self.admitted():
                raise Error("gpu_lease_lost", 503)
            payload = json.loads(row["payload"])
            args = payload["parameters"].copy()
            paths = self.references(row["owner"], payload)
            vpaths = self.video_references(row["owner"], payload)
            args.pop("reference_videos", None)
            args.pop("upscale", None)
            if paths:
                args["reference_images"] = paths
                if payload["capability"] == "video":
                    args["mode"] = "r2v"
            if payload["capability"] == "video" and payload["model"].startswith("veo-") and args.get("duration") == 8:
                args.pop("duration", None)
            requested_acct = payload.get("channel_account_id")
            active_clients = self._active_clients()
            if requested_acct:
                target_ids = [requested_acct]
            else:
                target_ids = [
                    a["id"] for a in self.q.list_accounts()
                    if a["enabled"] and a["status"] == "active" and a["id"] in active_clients
                ]
            if not target_ids:
                if requested_acct:
                    raise Error("channel_unavailable", 503)
                raise Error("channel_paused", 409)

            balances = {}
            unauthorized_ids = []
            for aid in target_ids:
                c = self.client_for(aid)
                try:
                    acct = await c.account()
                except Error:
                    if requested_acct:
                        raise
                    continue
                if not acct.get("authorized"):
                    unauthorized_ids.append(aid)
                    with self.q.transaction():
                        self.q.pause("authorization_required", call_id=row["id"], account_id=aid)
                    continue
                balances[aid] = acct["credits"]

            if not balances:
                if unauthorized_ids:
                    raise Error("authorization_required", 403)
                raise Error("channel_unavailable", 503)

            first_client = self.client_for(next(iter(balances)))
            catalog = await first_client.catalog()
            if payload["model"] not in catalog["models"][payload["capability"]]:
                raise Error("capability_not_supported")
            quote = first_client.quote(payload)
            if not self.admitted():
                raise Error("gpu_lease_lost", 503)
            chosen_id = self.q.admit_multi(row["id"], quote, balances, explicit_account_id=requested_acct)
            if not chosen_id:
                return
            slot_client = self.client_for(chosen_id)
            slot_output = self.output_for(chosen_id)
            try:
                if vpaths and payload["capability"] == "video":
                    result = await slot_client.generate_video_edit(
                        prompt=args["prompt"],
                        video_path=vpaths[0],
                        reference_images=paths,
                        aspect=args.get("aspect", "16:9"),
                        duration=args.get("duration"),
                        output_dir=slot_output,
                    )
                else:
                    result = await slot_client.tool("gflow_generate_" + payload["capability"], model=payload["model"], **args)
            except Error:
                result = {}
            media = result.get("flow_media_id")
            if isinstance(media, str) and ID.fullmatch(media):
                self.q.outcome(row["id"], True, media=media)
                await self.collect(self.q.find("id", row["id"]), result)
            else:
                error = result.get("error", {})
                error = error if isinstance(error, dict) else {}
                err_type = str(error.get("type", "")).lower()
                err_code = str(error.get("code", "")).lower()
                err_detail = str(error.get("detail", ""))
                unusual = (err_code == "unusual_activity"
                           or "PUBLIC_ERROR_UNUSUAL_ACTIVITY" in err_detail)
                upload_terms = (
                    "media-upload-rejected" in err_type
                    or err_code == "media_upload_rejected"
                    or "upload-terms" in err_detail.lower()
                )
                profile_locked = (
                    "profile-locked" in err_type
                    or err_code == "profile_locked"
                )
                rejected = unusual or upload_terms or profile_locked or error.get("charged") == "no" or error.get("accepted") is False
                if unusual:
                    reason = "unusual_activity"
                elif upload_terms:
                    reason = "upload_terms_required"
                elif profile_locked:
                    reason = "profile_locked"
                elif rejected:
                    reason = "rejected"
                else:
                    reason = "acceptance_unknown"
                self.q.outcome(row["id"], False if rejected else None, reason=reason)
        finally:
            self._inflight.discard(row["id"])

    async def collect(self, row, result=None):
        aid = row["account_id"] if "account_id" in row.keys() else "default"
        slot_client = self.client_for(aid)
        slot_output = self.output_for(aid)
        if not result or not result.get("files"):
            result = await slot_client.tool("gflow_download_media", media_id=row["media"])
            paths = [result.get("path")]
        else:
            paths = result["files"]
        if not paths or not paths[0]:
            return
        payload = json.loads(row["payload"])
        kind = payload["capability"]
        upscale = payload.get("parameters", {}).get("upscale")
        from pathlib import Path
        chosen_path = paths[0]
        upscale_status = None
        if upscale and row["media"]:
            up_tool = "gflow_upscale_video" if kind == "video" else "gflow_upscale_image"
            up_args = {"media_id": row["media"], "scale": upscale}
            if isinstance(result, dict) and isinstance(result.get("project_id"), str) and result["project_id"]:
                up_args["project"] = result["project_id"]
            try:
                up = await slot_client.tool(up_tool, **up_args)
                up_path = up.get("path") if isinstance(up, dict) else None
                if isinstance(up_path, str) and Path(up_path).is_file():
                    chosen_path = up_path
                    upscale_status = "upscaled"
                else:
                    upscale_status = "fallback_original"
            except Exception:
                upscale_status = "fallback_original"
        mime = "video/mp4" if kind == "video" else {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}.get(Path(chosen_path).suffix.lower(), "image/png")
        meta = self.media.collect(chosen_path, slot_output, mime)
        if upscale_status:
            meta["upscale_status"] = upscale_status
            meta["upscale_target"] = upscale
        spent = result.get("credits_used")
        confirmed = type(spent) is int and spent >= 0
        with self.q.transaction():
            current = self.q.find("id", row["id"])
            if current["state"] != "running" or current["revision"] != row["revision"]:
                return
            self.q.db.execute("INSERT OR REPLACE INTO results VALUES (?,?)", (row["id"], encode(meta)))
            self.q.db.execute("UPDATE calls SET state='succeeded',reason=?,spent=?,reserve=? WHERE id=?",
                              (None if confirmed else "charge_unconfirmed", spent if confirmed else 0,
                               0 if confirmed else current["reserve"], row["id"]))
            audit_detail = {"account_id": aid, "call_id": row["id"], "actual_credits": spent if confirmed else None}
            if upscale_status:
                audit_detail["upscale_status"] = upscale_status
                audit_detail["upscale_target"] = upscale
            self.q.audit("system", "succeeded", audit_detail)

    async def tick(self):
        import asyncio
        with self.q.transaction():
            if not self.q.any_active_account():
                return
            rows = self.q.db.execute("SELECT * FROM calls WHERE state IN ('pending','running') ORDER BY rowid").fetchall()
        async def advance(row):
            try:
                if row["state"] == "pending":
                    await self.dispatch(row)
                elif not self.q.db.execute("SELECT 1 FROM results WHERE id=?", (row["id"],)).fetchone():
                    await self.collect(row)
            except Error:
                return
        await asyncio.gather(*(advance(row) for row in rows))


def execution(g, token):
    async def handle(request):
        if request.url.path == "/health":
            return reply({"status": "ok"})
        bearer(request, token)
        owner = g.owner(request)
        path = request.url.path
        if path == "/v1/account":
            accts_summary = []
            total_credits = 0
            any_auth = False
            for a in g.q.list_accounts():
                info = {"authorized": None, "credits": None}
                try:
                    c = g.client_for(a["id"])
                    info = await c.account()
                except Error:
                    pass
                if a["enabled"] and a["status"] == "active" and info.get("authorized"):
                    any_auth = True
                    if isinstance(info.get("credits"), int):
                        total_credits += info["credits"]
                accts_summary.append({
                    "id": a["id"], "label": a["label"], "enabled": a["enabled"],
                    "status": a["status"], "priority": a["priority"],
                    "authorized": info.get("authorized"), "credits": info.get("credits"),
                })
            return reply({
                "authorized": any_auth,
                "credits": total_credits if any_auth else None,
                "accounts": accts_summary,
            })
        if path in ("/v1/capabilities", "/v1/models"):
            return reply(await g.any_client().catalog())
        if path == "/v1/inputs":
            digest = request.headers.get("x-content-sha256", "")
            meta = g.media.put(await body(request, MAX_SIZE), request.headers.get("content-type"), digest)
            with g.q.transaction():
                g.q.db.execute("INSERT OR IGNORE INTO inputs VALUES (?,?)", (owner, digest))
            return reply(meta)
        if request.method == "POST":
            data = await object_body(request)
            key, payload = g.normalize(data if path != "/v1/cost" else data | {"request_id": "quote"})
            if path == "/v1/cost":
                return reply({"credits": g.any_client().quote(payload)})
            g.references(owner, payload)
            g.video_references(owner, payload)
            row = g.q.intent(owner, key, payload)
            await g.dispatch(row)
            updated = g.q.find("id", row["id"], owner)
            res_row = g.q.db.execute("SELECT metadata FROM results WHERE id=?", (updated["id"],)).fetchone()
            return reply(view(updated, json.loads(res_row[0]) if res_row else None))
        lookup = request.path_params["value"]
        column = "key" if path.startswith(("/v1/jobs/", "/v1/calls/by-idempotency/")) else "id"
        row = g.q.find(column, lookup, owner)
        res_row = g.q.db.execute("SELECT metadata FROM results WHERE id=?", (row["id"],)).fetchone() if row else None
        meta = json.loads(res_row[0]) if res_row else None
        data = view(row, meta)
        if path.endswith(("/content", "/result")):
            if not meta:
                raise Error("result_not_ready", 409)
            raw = g.media.read(meta["sha256"])
            return Response(raw, media_type=meta["mime_type"], headers={"X-Content-SHA256": meta["sha256"], "Cache-Control": "no-store"})
        return reply(data)

    reads = ["/health", "/v1/account", "/v1/models", "/v1/capabilities", "/v1/jobs/{value}",
             "/v1/jobs/{value}/content", "/v1/calls/by-idempotency/{value}", "/v1/calls/{value}", "/v1/calls/{value}/result"]
    writes = ["/v1/jobs", "/v1/calls", "/v1/cost"]
    return Starlette(routes=[Route(path, handle) for path in reads] +
                     [Route(path, handle, methods=["POST"]) for path in writes] +
                     [Route("/v1/inputs", handle, methods=["PUT"])], exception_handlers={Error: handle_error})
