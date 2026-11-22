"""Atomic admission, multi-account ledger, and reconciliation. A single runtime owns this database."""
from contextlib import contextmanager
import json
import os
import re
import sqlite3
import threading
import time
from uuid import uuid4

from .policy import DEFAULTS, DEFAULT_ALLOWED_PROXY_HOSTS, Error, REASONS, validate, validate_proxy_url

DEFAULT_ACCOUNT_ID = "default"
BASE_SLOT_PORT = 18001
ACCOUNT_ID_RE = re.compile(r"^(?:default|acct_[0-9a-f]{8})$")


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _allowed_proxy_hosts():
    raw = os.environ.get("GOOGLE_FLOW_ALLOWED_PROXY_HOSTS", "")
    if not raw.strip():
        return DEFAULT_ALLOWED_PROXY_HOSTS
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _max_accounts():
    try:
        val = int(os.environ.get("GOOGLE_FLOW_MAX_ACCOUNTS", "8"))
        return max(1, min(val, 16))
    except ValueError:
        return 8


class Queue:
    def __init__(self, path, clock=time.time, allowed_proxy_hosts=None, max_accounts=None):
        self.clock = clock
        self.allowed_proxy_hosts = tuple(allowed_proxy_hosts) if allowed_proxy_hosts is not None else _allowed_proxy_hosts()
        self.max_accounts = max_accounts if max_accounts is not None else _max_accounts()
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS accounts (
                id TEXT PRIMARY KEY,
                label TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                priority INTEGER NOT NULL DEFAULT 100,
                proxy_url TEXT NOT NULL DEFAULT '',
                port INTEGER NOT NULL UNIQUE,
                policy_json TEXT NOT NULL,
                pause_json TEXT NOT NULL DEFAULT '{}',
                epoch INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS calls (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, key TEXT NOT NULL UNIQUE,
                payload TEXT NOT NULL, state TEXT NOT NULL, accepted INTEGER, reason TEXT,
                reserve INTEGER NOT NULL DEFAULT 0, spent INTEGER NOT NULL DEFAULT 0,
                attempt INTEGER NOT NULL DEFAULT 0, epoch INTEGER, started REAL,
                media TEXT, revision INTEGER NOT NULL DEFAULT 0,
                account_id TEXT NOT NULL DEFAULT 'default');
            CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY, time REAL, actor TEXT, action TEXT, detail TEXT);
        """)
        with self.transaction():
            # Smoothly migrate legacy single-account DBs missing calls.account_id
            cols = {row[1] for row in self.db.execute("PRAGMA table_info(calls)").fetchall()}
            if "account_id" not in cols:
                self.db.execute("ALTER TABLE calls ADD COLUMN account_id TEXT NOT NULL DEFAULT 'default'")
            acols = {row[1] for row in self.db.execute("PRAGMA table_info(accounts)").fetchall()}
            if "proxy_name" not in acols:
                self.db.execute("ALTER TABLE accounts ADD COLUMN proxy_name TEXT NOT NULL DEFAULT ''")
            if "proxy_id" not in acols:
                self.db.execute("ALTER TABLE accounts ADD COLUMN proxy_id TEXT NOT NULL DEFAULT ''")
            if "proxy_revision" not in acols:
                self.db.execute("ALTER TABLE accounts ADD COLUMN proxy_revision INTEGER NOT NULL DEFAULT 0")
            if "snapshot_json" not in acols:
                self.db.execute("ALTER TABLE accounts ADD COLUMN snapshot_json TEXT NOT NULL DEFAULT '{}'")
            for key, value in {"policy": DEFAULTS, "pause": {}, "epoch": 0}.items():
                self.db.execute("INSERT OR IGNORE INTO settings VALUES (?,?)", (key, encode(value)))
            # Smoothly migrate single-account settings into default account row
            if not self.db.execute("SELECT 1 FROM accounts LIMIT 1").fetchone():
                now = self.clock()
                self.db.execute(
                    """INSERT INTO accounts(id,label,enabled,priority,proxy_url,port,policy_json,pause_json,epoch,created_at,updated_at)
                       VALUES(?,?,1,10,'',?,?,?,?,?,?)""",
                    (
                        DEFAULT_ACCOUNT_ID,
                        "默认账号",
                        BASE_SLOT_PORT,
                        encode(self.setting("policy")),
                        encode(self.setting("pause")),
                        int(self.setting("epoch")),
                        now,
                        now,
                    ),
                )
            dispatching = self.db.execute("SELECT id, account_id FROM calls WHERE state='dispatching'").fetchall()
            if dispatching:
                self.db.execute("UPDATE calls SET state='submission_unknown',reason='acceptance_unknown' WHERE state='dispatching'")
                self.audit("system", "restart_reconciliation", {"count": len(dispatching)})
                for item in dispatching:
                    aid = item["account_id"] or DEFAULT_ACCOUNT_ID
                    if self.account_policy(aid)["on_submission_unknown"] == "pause":
                        self.pause("submission_unknown", account_id=aid, call_id=item["id"])

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

    # --- Multi-account management ---

    def _acct_row(self, account_id=DEFAULT_ACCOUNT_ID):
        row = self.db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        if not row:
            raise Error("account_not_found", 404, field="account_id")
        return row

    def list_accounts(self):
        rows = self.db.execute("SELECT * FROM accounts ORDER BY priority ASC, created_at ASC, id ASC").fetchall()
        return [self._format_account(r) for r in rows]

    def get_account(self, account_id=DEFAULT_ACCOUNT_ID):
        return self._format_account(self._acct_row(account_id))

    def load_account_snapshot(self, account_id=DEFAULT_ACCOUNT_ID):
        with self.lock:
            row = self.db.execute("SELECT snapshot_json FROM accounts WHERE id=?", (account_id,)).fetchone()
            if not row:
                return None
            raw = row["snapshot_json"] or "{}"
            try:
                data = json.loads(raw)
            except (TypeError, ValueError):
                return None
            if isinstance(data, dict) and data.get("authorized") is not None:
                return data
            return None

    def save_account_snapshot(self, account_id, snapshot):
        if not isinstance(snapshot, dict) or snapshot.get("authorized") is None:
            return
        with self.transaction():
            self.db.execute(
                "UPDATE accounts SET snapshot_json=? WHERE id=?",
                (encode(snapshot), account_id),
            )

    def _format_account(self, row):
        pause = json.loads(row["pause_json"] or "{}")
        if pause.get("until") is not None and self.clock() >= pause["until"]:
            pause = {}
        return {
            "id": row["id"],
            "label": row["label"],
            "enabled": bool(row["enabled"]),
            "priority": int(row["priority"]),
            "proxy_url": row["proxy_url"] or "",
            "proxy_name": row["proxy_name"] if "proxy_name" in row.keys() else "",
            "proxy_id": row["proxy_id"] if "proxy_id" in row.keys() else "",
            "proxy_revision": int(row["proxy_revision"]) if "proxy_revision" in row.keys() else 0,
            "port": int(row["port"]),
            "policy": json.loads(row["policy_json"]),
            "pause": pause,
            "status": "disabled" if not row["enabled"] else ("paused" if pause else "active"),
            "epoch": int(row["epoch"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def create_account(self, data, actor):
        if not isinstance(data, dict):
            raise Error("invalid_request", 400)
        label = data.get("label")
        if not isinstance(label, str) or not 1 <= len(label.strip()) <= 64:
            raise Error("invalid_field", field="label")
        label = label.strip()
        priority = data.get("priority", 100)
        if type(priority) is not int or not (1 <= priority <= 999):
            raise Error("invalid_field", field="priority")
        proxy_url = validate_proxy_url(data.get("proxy_url", ""), self.allowed_proxy_hosts)
        proxy_name = str(data.get("proxy_name") or "").strip()[:64]
        proxy_id = str(data.get("proxy_id") or "").strip()[:64]
        proxy_revision = int(data.get("proxy_revision") or 0)
        enabled = 1 if data.get("enabled", True) else 0
        with self.transaction():
            existing = self.db.execute("SELECT port FROM accounts").fetchall()
            if len(existing) >= self.max_accounts:
                raise Error("account_limit_reached", 409)
            used_ports = {int(r[0]) for r in existing}
            port = next((p for p in range(BASE_SLOT_PORT, BASE_SLOT_PORT + 64) if p not in used_ports), None)
            if port is None:
                raise Error("account_limit_reached", 409)
            account_id = "acct_" + uuid4().hex[:8]
            now = self.clock()
            self.db.execute(
                """INSERT INTO accounts(id,label,enabled,priority,proxy_url,proxy_name,proxy_id,proxy_revision,port,policy_json,pause_json,epoch,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,0,?,?)""",
                (account_id, label, enabled, priority, proxy_url, proxy_name, proxy_id, proxy_revision, port, encode(DEFAULTS), "{}", now, now),
            )
            self.audit(actor, "account_create", {"account_id": account_id, "label": label, "priority": priority,
                                                 "proxy_url": proxy_url, "proxy_name": proxy_name, "port": port})
            return self.get_account(account_id)

    def update_account(self, account_id, data, actor):
        if not isinstance(data, dict):
            raise Error("invalid_request", 400)
        with self.transaction():
            row = self._acct_row(account_id)
            label = row["label"]
            enabled = int(row["enabled"])
            priority = int(row["priority"])
            proxy_url = row["proxy_url"] or ""
            proxy_name = row["proxy_name"] if "proxy_name" in row.keys() else ""
            proxy_id = row["proxy_id"] if "proxy_id" in row.keys() else ""
            proxy_revision = int(row["proxy_revision"]) if "proxy_revision" in row.keys() else 0
            proxy_changed = False
            if "label" in data:
                if not isinstance(data["label"], str) or not 1 <= len(data["label"].strip()) <= 64:
                    raise Error("invalid_field", field="label")
                label = data["label"].strip()
            if "enabled" in data:
                if type(data["enabled"]) is not bool:
                    raise Error("invalid_field", field="enabled")
                enabled = 1 if data["enabled"] else 0
            if "priority" in data:
                if type(data["priority"]) is not int or not (1 <= data["priority"] <= 999):
                    raise Error("invalid_field", field="priority")
                priority = data["priority"]
            if "proxy_url" in data:
                new_proxy = validate_proxy_url(data["proxy_url"], self.allowed_proxy_hosts)
                if new_proxy != proxy_url:
                    inflight = self.db.execute(
                        "SELECT COUNT(*) FROM calls WHERE account_id=? AND state IN ('dispatching','running')",
                        (account_id,),
                    ).fetchone()[0]
                    if inflight > 0:
                        raise Error("account_busy", 409)
                    proxy_url = new_proxy
                    proxy_changed = True
                if "proxy_name" in data:
                    proxy_name = str(data.get("proxy_name") or "").strip()[:64]
                if "proxy_id" in data:
                    proxy_id = str(data.get("proxy_id") or "").strip()[:64]
                if "proxy_revision" in data:
                    proxy_revision = int(data.get("proxy_revision") or 0)
            self.db.execute(
                "UPDATE accounts SET label=?, enabled=?, priority=?, proxy_url=?, proxy_name=?, proxy_id=?, proxy_revision=?, updated_at=? WHERE id=?",
                (label, enabled, priority, proxy_url, proxy_name, proxy_id, proxy_revision, self.clock(), account_id),
            )
            self.audit(actor, "account_update", {
                "account_id": account_id, "label": label, "enabled": bool(enabled),
                "priority": priority, "proxy_url": proxy_url, "proxy_name": proxy_name, "proxy_changed": proxy_changed,
            })
            return self.get_account(account_id), proxy_changed

    def delete_account(self, account_id, actor):
        if account_id == DEFAULT_ACCOUNT_ID:
            raise Error("cannot_delete_default_account", 409)
        with self.transaction():
            row = self._acct_row(account_id)
            if row["enabled"]:
                raise Error("account_must_be_disabled", 409)
            active = self.db.execute(
                "SELECT COUNT(*) FROM calls WHERE account_id=? AND state IN ('pending','dispatching','running','submission_unknown')",
                (account_id,),
            ).fetchone()[0]
            if active > 0:
                raise Error("account_busy", 409)
            self.db.execute("DELETE FROM accounts WHERE id=?", (account_id,))
            self.audit(actor, "account_delete", {"account_id": account_id, "label": row["label"]})
            return {"deleted": True, "id": account_id}

    def account_policy(self, account_id=DEFAULT_ACCOUNT_ID):
        return json.loads(self._acct_row(account_id)["policy_json"])

    def configure(self, changes, actor, account_id=DEFAULT_ACCOUNT_ID):
        validate(changes)
        with self.transaction():
            old = self.account_policy(account_id)
            merged = old | changes
            self.db.execute("UPDATE accounts SET policy_json=?, updated_at=? WHERE id=?",
                            (encode(merged), self.clock(), account_id))
            if account_id == DEFAULT_ACCOUNT_ID:
                self.save("policy", merged)
            self.audit(actor, "policy", {"account_id": account_id, "old": old, "new": merged})

    def pause(self, reason, actor="system", call_id=None, account_id=DEFAULT_ACCOUNT_ID):
        # Called inside the caller's state transaction.
        if reason not in REASONS:
            raise Error("invalid_reason")
        row = self._acct_row(account_id)
        previous = json.loads(row["pause_json"] or "{}")
        if previous.get("reason") == "gpu_lease_lost" and reason != "gpu_lease_lost":
            return
        policy = json.loads(row["policy_json"])
        minutes = policy["cooldown_minutes"] if reason == "unusual_activity" else 0
        state = {"reason": reason, "since": self.clock(), "call_id": call_id,
                 "until": self.clock() + 60 * minutes if minutes else None}
        self.db.execute("UPDATE accounts SET pause_json=?, updated_at=? WHERE id=?",
                        (encode(state), self.clock(), account_id))
        if account_id == DEFAULT_ACCOUNT_ID:
            self.save("pause", state)
        self.audit(actor, "pause", {"account_id": account_id, "old": previous or "active", "new": state})

    def resume(self, actor, lease_valid=False, account_id=DEFAULT_ACCOUNT_ID):
        row = self._acct_row(account_id)
        previous = json.loads(row["pause_json"] or "{}")
        if previous.get("reason") == "gpu_lease_lost" and not lease_valid:
            raise Error("invalid_field", field="gpu_lease")
        self.db.execute("UPDATE accounts SET pause_json='{}', updated_at=? WHERE id=?",
                        (self.clock(), account_id))
        if account_id == DEFAULT_ACCOUNT_ID:
            self.save("pause", {})
        self.audit(actor, "resume", {"account_id": account_id, "old": previous, "new": "active"})

    def status(self, account_id=DEFAULT_ACCOUNT_ID):
        row = self._acct_row(account_id)
        paused = json.loads(row["pause_json"] or "{}")
        if paused.get("until") is not None and self.clock() >= paused["until"]:
            self.resume("system", account_id=account_id)
            paused = {}
        return {"status": "paused" if paused else "active", **paused}

    def any_active_account(self):
        for acct in self.list_accounts():
            if acct["enabled"] and self.status(acct["id"])["status"] == "active":
                return True
        return False

    def action(self, name, actor, lease_valid=False, account_id=DEFAULT_ACCOUNT_ID):
        with self.transaction():
            row = self._acct_row(account_id)
            if name == "pause":
                self.pause("manual", actor, account_id=account_id)
            elif name == "resume":
                self.resume(actor, lease_valid, account_id=account_id)
            elif name == "clear-cooldown":
                state = json.loads(row["pause_json"] or "{}")
                if state:
                    updated = state | {"until": None}
                    self.db.execute("UPDATE accounts SET pause_json=?, updated_at=? WHERE id=?",
                                    (encode(updated), self.clock(), account_id))
                    if account_id == DEFAULT_ACCOUNT_ID:
                        self.save("pause", updated)
                self.audit(actor, name, {"account_id": account_id})
            elif name == "reset-count":
                new_epoch = int(row["epoch"]) + 1
                self.db.execute("UPDATE accounts SET epoch=?, updated_at=? WHERE id=?",
                                (new_epoch, self.clock(), account_id))
                if account_id == DEFAULT_ACCOUNT_ID:
                    self.save("epoch", new_epoch)
                self.audit(actor, name, {"account_id": account_id})
            else:
                raise Error("unknown_action")

    def usage(self, account_id=None):
        day = int(self.clock() // 86400) * 86400
        if account_id is None:
            row = self.db.execute("""SELECT COALESCE(SUM(c.accepted IS NOT 0),0), COALESCE(SUM(c.spent),0)
                FROM calls c JOIN accounts a ON a.id = c.account_id
                WHERE c.started>=? AND c.epoch=a.epoch""", (day,)).fetchone()
            reserved = self.db.execute("SELECT COALESCE(SUM(reserve),0) FROM calls").fetchone()[0]
            return {"submits": row[0], "credits": row[1], "reserved": reserved}
        acct_epoch = int(self._acct_row(account_id)["epoch"])
        row = self.db.execute("""SELECT COALESCE(SUM(accepted IS NOT 0),0), COALESCE(SUM(spent),0)
            FROM calls WHERE account_id=? AND started>=? AND epoch=?""", (account_id, day, acct_epoch)).fetchone()
        reserved = self.db.execute("SELECT COALESCE(SUM(reserve),0) FROM calls WHERE account_id=?", (account_id,)).fetchone()[0]
        return {"submits": row[0], "credits": row[1], "reserved": reserved}

    def running_count(self, account_id=None):
        if account_id is None:
            return self.db.execute("SELECT COUNT(*) FROM calls WHERE state IN ('dispatching','running')").fetchone()[0]
        return self.db.execute(
            "SELECT COUNT(*) FROM calls WHERE account_id=? AND state IN ('dispatching','running')",
            (account_id,),
        ).fetchone()[0]

    def find(self, column, value, owner=None):
        if column not in ("id", "key"):
            raise ValueError("invalid lookup")
        row = self.db.execute(f"SELECT * FROM calls WHERE {column}=?", (value,)).fetchone()
        if row and owner is not None and row["owner"] != owner:
            raise Error("forbidden", 403)
        return row

    def intent(self, owner, key, payload):
        requested_account = payload.get("channel_account_id")
        with self.transaction():
            row = self.find("key", key, owner)
            if row:
                if row["payload"] != encode(payload):
                    raise Error("idempotency_conflict", 409)
                return row
            if requested_account:
                acct = self.get_account(requested_account)
                if not acct["enabled"] or self.status(requested_account)["status"] == "paused":
                    raise Error("channel_paused", 409)
                target_account = requested_account
            else:
                if not self.any_active_account():
                    raise Error("channel_paused", 409)
                target_account = DEFAULT_ACCOUNT_ID
            call_id = uuid4().hex
            self.db.execute(
                "INSERT INTO calls(id,owner,key,payload,state,accepted,account_id) VALUES(?,?,?,?,'pending',0,?)",
                (call_id, owner, key, encode(payload), target_account),
            )
            return self.find("id", call_id)

    def _can_admit_account(self, acct_row, quote, balance, cap):
        aid = acct_row["id"]
        if not acct_row["enabled"]:
            return False, "channel_paused"
        if self.status(aid)["status"] != "active":
            return False, "channel_paused"
        policy = json.loads(acct_row["policy_json"])
        usage = self.usage(aid)
        running, last = self.db.execute(
            "SELECT COALESCE(SUM(state IN ('dispatching','running')),0),MAX(started) FROM calls WHERE account_id=?",
            (aid,),
        ).fetchone()
        if running >= policy["concurrency"] or (last is not None and self.clock() - last < policy["min_interval_seconds"]):
            return False, "busy"
        if policy["daily_submit_limit"] and usage["submits"] >= policy["daily_submit_limit"]:
            return False, "busy"
        if cap == 0 or (cap is not None and quote > cap):
            return False, "quota_exhausted"
        if policy["per_job_credit_limit"] and quote > policy["per_job_credit_limit"]:
            return False, "quota_exhausted"
        if (quote + usage["reserved"] > balance or
                (policy["daily_credit_limit"] and quote + usage["credits"] + usage["reserved"] > policy["daily_credit_limit"])):
            return False, "credit_limit"
        return True, (int(acct_row["priority"]), int(running), float(last or 0.0), -(balance - usage["reserved"]), aid)

    def admit(self, call_id, quote, balance, account_id=None):
        """Legacy single-account admit signature preserved for backwards compatibility."""
        aid = account_id or DEFAULT_ACCOUNT_ID
        return self.admit_multi(call_id, quote, {aid: balance}, explicit_account_id=aid) is not None

    def admit_multi(self, call_id, quote, balances_by_account, explicit_account_id=None):
        """Atomically select and bind the best eligible account (or strict explicit account) for a pending call."""
        with self.transaction():
            row = self.find("id", call_id)
            if not row or row["state"] != "pending":
                return None
            payload = json.loads(row["payload"])
            cap = payload.get("budget_limit")
            strict_id = explicit_account_id or payload.get("channel_account_id")
            if strict_id:
                acct_row = self._acct_row(strict_id)
                if strict_id not in balances_by_account:
                    return None
                ok, info = self._can_admit_account(acct_row, quote, balances_by_account[strict_id], cap)
                if not ok:
                    if info == "credit_limit":
                        self.pause("credit_limit", call_id=call_id, account_id=strict_id)
                        self.db.execute("UPDATE calls SET state='failed',reason='quota_exhausted',account_id=? WHERE id=?",
                                        (strict_id, call_id))
                    elif info == "quota_exhausted":
                        self.db.execute("UPDATE calls SET state='failed',reason='quota_exhausted',account_id=? WHERE id=?",
                                        (strict_id, call_id))
                    return None
                chosen_id = strict_id
            else:
                acct_rows = self.db.execute(
                    "SELECT * FROM accounts WHERE enabled=1 ORDER BY priority ASC, created_at ASC, id ASC"
                ).fetchall()
                candidates = []
                terminal_reasons = []
                for acct_row in acct_rows:
                    aid = acct_row["id"]
                    if aid not in balances_by_account:
                        continue
                    ok, info = self._can_admit_account(acct_row, quote, balances_by_account[aid], cap)
                    if ok:
                        candidates.append((info, aid))
                    else:
                        terminal_reasons.append((aid, info))
                if not candidates:
                    # Only fail immediately if all known-balance accounts failed due to hard quota/credit limits (not transient busy)
                    if terminal_reasons and all(r in ("quota_exhausted", "credit_limit") for _, r in terminal_reasons):
                        for aid, r in terminal_reasons:
                            if r == "credit_limit":
                                self.pause("credit_limit", call_id=call_id, account_id=aid)
                        self.db.execute("UPDATE calls SET state='failed',reason='quota_exhausted' WHERE id=?", (call_id,))
                    return None
                candidates.sort(key=lambda item: item[0])
                chosen_id = candidates[0][1]

            chosen_epoch = int(self._acct_row(chosen_id)["epoch"])
            self.db.execute(
                """UPDATE calls SET state='dispatching',accepted=NULL,reserve=?,
                   epoch=?,started=?,attempt=attempt+1,account_id=? WHERE id=?""",
                (quote, chosen_epoch, self.clock(), chosen_id, call_id),
            )
            return chosen_id

    def outcome(self, call_id, accepted, *, reason=None, media=None, spent=None):
        with self.transaction():
            row = self.find("id", call_id)
            if row["state"] not in ("dispatching", "running"):
                return
            aid = row["account_id"] or DEFAULT_ACCOUNT_ID
            policy = self.account_policy(aid)
            if accepted is None:
                state = "submission_unknown"
                if policy["on_submission_unknown"] == "pause":
                    self.pause(state, call_id=call_id, account_id=aid)
            elif not accepted:
                state = "failed"
                if reason in ("authorization_required", "upload_terms_required") or (reason == "unusual_activity" and
                        policy["on_unusual_activity"] == "pause"):
                    self.pause(reason, call_id=call_id, account_id=aid)
            else:
                state = "succeeded" if spent is not None else "running"
            self.db.execute("""UPDATE calls SET state=?,accepted=?,reason=?,media=?,spent=?,reserve=? WHERE id=?""",
                            (state, accepted, reason, media or row["media"], spent or 0,
                             0 if state in ("failed", "succeeded") else row["reserve"], call_id))
            self.audit("system", state, {"account_id": aid, "call_id": call_id, "reason": reason})

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
            self.audit(actor, "reconcile", {"account_id": row["account_id"], "action": action,
                                            "call_id": call_id, "revision": revision})
