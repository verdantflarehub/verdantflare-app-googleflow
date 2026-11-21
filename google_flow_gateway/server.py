import hmac
import json
import re
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .gflow_client import MODELS, VIDEO_MODEL_LIMITS
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


def view(row):
    if row is None:
        raise Error("not_found", 404)
    accepted = None if row["accepted"] is None else bool(row["accepted"])
    payload = json.loads(row["payload"])
    return dict(call_id=row["id"], request_id=row["key"], accepted=accepted, state=row["state"],
                started=row["started"], model=payload.get("model"), capability=payload.get("capability"),
                status=row["state"], reason_code=row["reason"], revision=row["revision"],
                reserved=row["reserve"], credits=None if row["reason"] == "charge_unconfirmed" else row["spent"])


class Gateway:
    def __init__(self, queue, client, media, output, org):
        self.q, self.client, self.media, self.output, self.org = queue, client, media, output, org
        self.admitted = lambda: True
        self._inflight = set()
        self.q.db.executescript("""
            CREATE TABLE IF NOT EXISTS inputs(owner TEXT, digest TEXT, PRIMARY KEY(owner,digest));
            CREATE TABLE IF NOT EXISTS results(id TEXT PRIMARY KEY, metadata TEXT);
        """)

    def owner(self, request):
        org = header(request, "x-organization-id")
        if org != self.org or request.headers.getlist("x-project-write") != ["true"]:
            raise Error("forbidden", 403)
        return encode([org, header(request, "x-project-id"), header(request, "x-user-id")])

    def normalize(self, data):
        if set(data) - {"capability", "model", "parameters", "budget_limit", "idempotency_key", "request_id"}:
            raise Error("invalid_field")
        key = data.get("idempotency_key", data.get("request_id"))
        if not isinstance(key, str) or not ID.fullmatch(key) or data.get("request_id", key) != key:
            raise Error("invalid_field", field="idempotency_key")
        kind, model, args = data.get("capability"), data.get("model"), data.get("parameters")
        if not isinstance(kind, str) or not isinstance(model, str) or model not in MODELS.get(kind, ()):
            raise Error("capability_not_supported")
        if not isinstance(args, dict) or set(args) - {"prompt", "aspect", "reference_images", "reference_videos", "duration"}:
            raise Error("invalid_field", field="parameters")
        if not isinstance(args.get("prompt"), str) or not 1 <= len(args["prompt"].strip()) <= 5000:
            raise Error("invalid_field", field="prompt")
        if args.get("aspect", "16:9") not in (("16:9", "9:16", "1:1") if kind == "image" else ("16:9", "9:16")):
            raise Error("invalid_field", field="aspect")
        if "duration" in args:
            allowed_durations = VIDEO_MODEL_LIMITS.get(model, {}).get("durations", (4, 6, 8))
            if kind != "video" or args["duration"] not in allowed_durations:
                raise Error("invalid_field", field="duration")
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
        return key, dict(capability=kind, model=model, parameters=args, budget_limit=limit)

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
            if paths:
                args["reference_images"] = paths
                if payload["capability"] == "video":
                    args["mode"] = "r2v"
            if payload["capability"] == "video" and payload["model"].startswith("veo-") and args.get("duration") == 8:
                args.pop("duration", None)
            acct = await self.client.account()
            if not acct["authorized"]:
                with self.q.transaction():
                    self.q.pause("authorization_required", call_id=row["id"])
                raise Error("authorization_required", 403)
            catalog = await self.client.catalog()
            if payload["model"] not in catalog["models"][payload["capability"]]:
                raise Error("capability_not_supported")
            quote = self.client.quote(payload)
            if not self.admitted():
                raise Error("gpu_lease_lost", 503)
            if not self.q.admit(row["id"], quote, acct["credits"]):
                return
            try:
                if vpaths and payload["capability"] == "video":
                    result = await self.client.generate_video_edit(
                        prompt=args["prompt"],
                        video_path=vpaths[0],
                        reference_images=paths,
                        aspect=args.get("aspect", "16:9"),
                        duration=args.get("duration"),
                        output_dir=self.output,
                    )
                else:
                    result = await self.client.tool("gflow_generate_" + payload["capability"], model=payload["model"], **args)
            except Error:
                result = {}
            media = result.get("flow_media_id")
            if isinstance(media, str) and ID.fullmatch(media):
                self.q.outcome(row["id"], True, media=media)
                await self.collect(self.q.find("id", row["id"]), result)
            else:
                error = result.get("error", {})
                error = error if isinstance(error, dict) else {}
                unusual = (str(error.get("code", "")).lower() == "unusual_activity"
                           or "PUBLIC_ERROR_UNUSUAL_ACTIVITY" in str(error.get("detail", "")))
                rejected = unusual or error.get("charged") == "no" or error.get("accepted") is False
                self.q.outcome(row["id"], False if rejected else None,
                               reason="unusual_activity" if unusual else "rejected" if rejected else "acceptance_unknown")
        finally:
            self._inflight.discard(row["id"])

    async def collect(self, row, result=None):
        if not result or not result.get("files"):
            result = await self.client.tool("gflow_download_media", media_id=row["media"])
            paths = [result.get("path")]
        else:
            paths = result["files"]
        if not paths or not paths[0]:
            return
        kind = json.loads(row["payload"])["capability"]
        from pathlib import Path
        mime = "video/mp4" if kind == "video" else {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}.get(Path(paths[0]).suffix.lower(), "image/png")
        meta = self.media.collect(paths[0], self.output, mime)
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
            self.q.audit("system", "succeeded", {"call_id": row["id"], "actual_credits": spent if confirmed else None})

    async def tick(self):
        import asyncio
        with self.q.transaction():
            if self.q.status()["status"] != "active":
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
            return reply(await g.client.account())
        if path in ("/v1/capabilities", "/v1/models"):
            return reply(await g.client.catalog())
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
                return reply({"credits": g.client.quote(payload)})
            g.references(owner, payload)
            g.video_references(owner, payload)
            row = g.q.intent(owner, key, payload)
            await g.dispatch(row)
            return reply(view(g.q.find("id", row["id"], owner)))
        lookup = request.path_params["value"]
        column = "key" if path.startswith(("/v1/jobs/", "/v1/calls/by-idempotency/")) else "id"
        row = g.q.find(column, lookup, owner)
        data = view(row)
        if path.endswith(("/content", "/result")):
            result = g.q.db.execute("SELECT metadata FROM results WHERE id=?", (row["id"],)).fetchone()
            if not result:
                raise Error("result_not_ready", 409)
            meta = json.loads(result[0])
            raw = g.media.read(meta["sha256"])
            return Response(raw, media_type=meta["mime_type"], headers={"X-Content-SHA256": meta["sha256"], "Cache-Control": "no-store"})
        return reply(data)

    reads = ["/health", "/v1/account", "/v1/models", "/v1/capabilities", "/v1/jobs/{value}",
             "/v1/jobs/{value}/content", "/v1/calls/by-idempotency/{value}", "/v1/calls/{value}", "/v1/calls/{value}/result"]
    writes = ["/v1/jobs", "/v1/calls", "/v1/cost"]
    return Starlette(routes=[Route(path, handle) for path in reads] +
                     [Route(path, handle, methods=["POST"]) for path in writes] +
                     [Route("/v1/inputs", handle, methods=["PUT"])], exception_handlers={Error: handle_error})
