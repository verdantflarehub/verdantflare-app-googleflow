"""Exercise the sibling Video publisher, replacing only task storage and Artifact HTTP."""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import runpy
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import httpx


class TaskStore:
    def __init__(self, path):
        self.path = path

    def read(self):
        return SimpleNamespace(**json.loads(self.path.read_text()))

    def update(self, record, **changes):
        self.path.write_text(json.dumps(vars(record) | changes))
        return self.read()


def check_response_loss(test, root, stage, path):
    source = Path(__file__).resolve().parents[2] / "verdantflare-app-video/services/video-mcp-server/app/artifact_output.py"
    publisher = runpy.run_path(str(source))
    real_client = httpx.Client
    state = {"uploads": {}, "versions": {}, "faults": 0}
    raw = path.read_bytes()
    meta = {"sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw)}
    source_info = {"kind":"task_output", "service_id":"video", "project_id":"project", "run_id":"video-task","task_id":"runtime-task"}

    def respond(request):
        route = request.url.path
        if route.endswith("/uploads"):
            phase = "prepare"
            body = json.loads(request.content)
            test.assertEqual(body["source"], source_info)
            upload_id = state["uploads"].setdefault(body["write_id"], str(uuid4()))
            answer = {"upload_id": upload_id}
        elif request.method == "PUT":
            phase = "put"
            test.assertIn(route.split("/")[-2], state["uploads"].values())
            test.assertEqual(request.content, raw)
            answer = {}
        elif route.endswith("/commit"):
            phase = "commit"
            upload_id = route.split("/")[-2]
            test.assertIn(upload_id, state["uploads"].values())
            ref = state["versions"].setdefault(upload_id, {k:publisher["uuid7"]() for k in ("store_id","artifact_id","version_id")})
            answer = dict(ref, **meta, mime="video/mp4", organization_id="org", created_by="user", source=source_info)
        elif route.endswith("/content"):
            return httpx.Response(200, content=raw)
        else:
            test.assertIn(route.split("/")[-1], state["uploads"].values())
            return httpx.Response(200, json={"state":"committed" if state["versions"] else "prepared"})
        if phase == stage and not state["faults"]:
            state["faults"] += 1
            raise httpx.ReadTimeout("fixture lost response")
        return httpx.Response(200, json=answer)

    tasks = TaskStore(root / "video-task.json")
    tasks.update(SimpleNamespace(project_id="project", video_task_id="video-task", runtime_task_id="runtime-task",
                                 authority={"organization_id":"org","user_id":"user"}, provider_state={}))
    transport = httpx.MockTransport(respond)
    def client(**kwargs):
        return real_client(transport=transport, **kwargs)
    with patch.dict(os.environ, {"VIDEO_ARTIFACT_SERVICE_URL":"http://fixture.invalid",
                    "VIDEO_ARTIFACT_SERVICE_TOKEN":os.urandom(32).hex(), "VIDEO_ARTIFACT_CA_FILE":""}), patch.object(httpx,"Client",client):
        with test.assertRaises(httpx.ReadTimeout):
            publisher["publish"](tasks, tasks.read(), path, meta)
        before = tasks.read().provider_state
        final = publisher["publish"](tasks, tasks.read(), path, meta)
    test.assertEqual(final.status, "succeeded")
    test.assertEqual(final.provider_state["artifact_write_id"], before["artifact_write_id"])
    test.assertEqual(set(state["uploads"].values()), {final.provider_state["artifact_upload_id"]})
    test.assertEqual((len(state["uploads"]), len(state["versions"]), state["faults"]), (1,1,1))
