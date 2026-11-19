"""Atomic admission and reconciliation. A single runtime owns this database."""
from contextlib import contextmanager
import json
import sqlite3
import threading
import time
from uuid import uuid4

from .policy import DEFAULTS, Error, REASONS, validate


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class Queue:
    def __init__(self, path, clock=time.time):
        self.clock = clock
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS calls (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, key TEXT NOT NULL UNIQUE,
                payload TEXT NOT NULL, state TEXT NOT NULL, accepted INTEGER, reason TEXT,
                reserve INTEGER NOT NULL DEFAULT 0, spent INTEGER NOT NULL DEFAULT 0,
                attempt INTEGER NOT NULL DEFAULT 0, epoch INTEGER, started REAL,
                media TEXT, revision INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY, time REAL, actor TEXT, action TEXT, detail TEXT);
        """)
        with self.transaction():
            for key, value in {"policy": DEFAULTS, "pause": {}, "epoch": 0}.items():
                self.db.execute("INSERT OR IGNORE INTO settings VALUES (?,?)", (key, encode(value)))
            if self.db.execute("SELECT 1 FROM calls WHERE state='dispatching'").fetchone():
                self.db.execute("UPDATE calls SET state='submission_unknown',reason='acceptance_unknown' WHERE state='dispatching'")
                self.audit("system", "restart_reconciliation", {})
                if self.setting("policy")["on_submission_unknown"] == "pause":
                    self.pause("submission_unknown")

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self.db.rollback()
                raise
            else:
                self.db.commit()

    def setting(self, key):
        return json.loads(self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()[0])

    def save(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, encode(value)))

    def audit(self, actor, action, detail):
        self.db.execute("INSERT INTO audit(time,actor,action,detail) VALUES (?,?,?,?)",
                        (self.clock(), actor, action, encode(detail)))

    def configure(self, changes, actor):
        validate(changes)
        with self.transaction():
            old = self.setting("policy")
            self.save("policy", old | changes)
            self.audit(actor, "policy", {"old": old, "new": old | changes})

    def pause(self, reason, actor="system", call_id=None):
        # Called inside the caller's state transaction.
        if reason not in REASONS:
            raise Error("invalid_reason")
        previous = self.setting("pause")
        if previous.get("reason") == "gpu_lease_lost" and reason != "gpu_lease_lost":
            return
        minutes = self.setting("policy")["cooldown_minutes"] if reason == "unusual_activity" else 0
        state = {"reason": reason, "since": self.clock(), "call_id": call_id,
                 "until": self.clock() + 60 * minutes if minutes else None}
        self.save("pause", state)
        self.audit(actor, "pause", {"old": previous or "active", "new": state})

    def resume(self, actor, lease_valid=False):
        previous = self.setting("pause")
        if previous.get("reason") == "gpu_lease_lost" and not lease_valid:
            raise Error("invalid_field", field="gpu_lease")
        self.save("pause", {})
        self.audit(actor, "resume", {"old": previous, "new": "active"})

    def status(self):
        paused = self.setting("pause")
        if paused.get("until") is not None and self.clock() >= paused["until"]:
            self.resume("system")
            paused = {}
        return {"status": "paused" if paused else "active", **paused}

    def action(self, name, actor, lease_valid=False):
        with self.transaction():
            if name == "pause":
                self.pause("manual", actor)
            elif name == "resume":
                self.resume(actor, lease_valid)
            elif name == "clear-cooldown":
                state = self.setting("pause")
                if state:
                    self.save("pause", state | {"until": None})
                self.audit(actor, name, {})
            elif name == "reset-count":
                self.save("epoch", self.setting("epoch") + 1)
                self.audit(actor, name, {})
            else:
                raise Error("unknown_action")

    def usage(self):
        day = int(self.clock() // 86400) * 86400
        row = self.db.execute("""SELECT COALESCE(SUM(accepted IS NOT 0),0), COALESCE(SUM(spent),0)
            FROM calls WHERE started>=? AND epoch=?""", (day, self.setting("epoch"))).fetchone()
        reserved = self.db.execute("SELECT COALESCE(SUM(reserve),0) FROM calls").fetchone()[0]
        return {"submits": row[0], "credits": row[1], "reserved": reserved}

    def find(self, column, value, owner=None):
        if column not in ("id", "key"):
            raise ValueError("invalid lookup")
        row = self.db.execute(f"SELECT * FROM calls WHERE {column}=?", (value,)).fetchone()
        if row and owner is not None and row["owner"] != owner:
            raise Error("forbidden", 403)
        return row

    def intent(self, owner, key, payload):
        with self.transaction():
            row = self.find("key", key, owner)
            if row:
                if row["payload"] != encode(payload):
                    raise Error("idempotency_conflict", 409)
                return row
            if self.status()["status"] == "paused":
                raise Error("channel_paused", 409)
            call_id = uuid4().hex
            self.db.execute("INSERT INTO calls(id,owner,key,payload,state,accepted) VALUES(?,?,?,?,'pending',0)",
                            (call_id, owner, key, encode(payload)))
            return self.find("id", call_id)

    def admit(self, call_id, quote, balance):
        with self.transaction():
            row = self.find("id", call_id)
            if row["state"] != "pending" or self.status()["status"] != "active":
                return False
            policy, usage = self.setting("policy"), self.usage()
            running, last = self.db.execute(
                "SELECT COALESCE(SUM(state IN ('dispatching','running')),0),MAX(started) FROM calls").fetchone()
            if running >= policy["concurrency"] or (last is not None and
                    self.clock() - last < policy["min_interval_seconds"]):
                return False
            if policy["daily_submit_limit"] and usage["submits"] >= policy["daily_submit_limit"]:
                return False
            cap = json.loads(row["payload"]).get("budget_limit")
            reason = None
            if cap == 0 or (cap is not None and quote > cap):
                reason = "quota_exhausted"
            elif policy["per_job_credit_limit"] and quote > policy["per_job_credit_limit"]:
                reason = "quota_exhausted"
            elif (quote + usage["reserved"] > balance or
                  (policy["daily_credit_limit"] and quote + usage["credits"] + usage["reserved"] > policy["daily_credit_limit"])):
                self.pause("credit_limit", call_id=call_id)
                reason = "quota_exhausted"
            if reason:
                self.db.execute("UPDATE calls SET state='failed',reason=? WHERE id=?", (reason, call_id))
                return False
            self.db.execute("""UPDATE calls SET state='dispatching',accepted=NULL,reserve=?,
                epoch=?,started=?,attempt=attempt+1 WHERE id=?""", (quote, self.setting("epoch"), self.clock(), call_id))
            return True

    def outcome(self, call_id, accepted, *, reason=None, media=None, spent=None):
        with self.transaction():
            row = self.find("id", call_id)
            if row["state"] not in ("dispatching", "running"):
                return
            if accepted is None:
                state = "submission_unknown"
                if self.setting("policy")["on_submission_unknown"] == "pause":
                    self.pause(state, call_id=call_id)
            elif not accepted:
                state = "failed"
                if reason == "authorization_required" or (reason == "unusual_activity" and
                        self.setting("policy")["on_unusual_activity"] == "pause"):
                    self.pause(reason, call_id=call_id)
            else:
                state = "succeeded" if spent is not None else "running"
            self.db.execute("""UPDATE calls SET state=?,accepted=?,reason=?,media=?,spent=?,reserve=? WHERE id=?""",
                            (state, accepted, reason, media or row["media"], spent or 0,
                             0 if state in ("failed", "succeeded") else row["reserve"], call_id))
            self.audit("system", state, {"call_id": call_id, "reason": reason})

    def reconcile(self, call_id, revision, action, actor, media=None):
        with self.transaction():
            row = self.find("id", call_id)
            if not row:
                raise Error("not_found", 404)
            if row["revision"] != revision or row["state"] != "submission_unknown":
                raise Error("revision_conflict", 409)
            if action == "bind" and media:
                state, accepted, reserve = "running", True, row["reserve"]
            elif action in ("fail", "retry"):
                state, accepted, reserve = ("failed" if action == "fail" else "pending"), False, 0
            else:
                raise Error("invalid_field", field="action")
            self.db.execute("""UPDATE calls SET revision=revision+1,state=?,accepted=?,reserve=?,media=?,reason=NULL
                            WHERE id=?""", (state, accepted, reserve, media, call_id))
            self.audit(actor, "reconcile", {"action": action, "call_id": call_id, "revision": revision})
