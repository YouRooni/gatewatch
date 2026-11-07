import asyncio
import ipaddress
import json
import re
import time
import uuid

from .database import script
from .targets import ActionTarget

ACTION_TTL = 180
UNCERTAIN_RESULTS = {"Fail2ban timeout", "Action timed out"}


class HubControl:
    def __init__(self, store):
        self.store, self.db = store, store.db
        self.signals = {}
        script(
            self.db,
            """
            CREATE TABLE IF NOT EXISTS actions(
                id TEXT PRIMARY KEY,node TEXT NOT NULL,action TEXT NOT NULL,ip TEXT NOT NULL,
                requested_by INTEGER NOT NULL,request_key TEXT UNIQUE NOT NULL,channel TEXT NOT NULL,
                context TEXT NOT NULL,created REAL NOT NULL,expires REAL NOT NULL,leased REAL DEFAULT 0,
                result TEXT,notified INTEGER DEFAULT 0,next_notify REAL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS actions_node ON actions(node,result,leased);
            CREATE INDEX IF NOT EXISTS actions_notify ON actions(channel,notified,next_notify);
            CREATE INDEX IF NOT EXISTS actions_target ON actions(node,ip,created);
            CREATE TABLE IF NOT EXISTS action_subscribers(
                job TEXT NOT NULL,channel TEXT NOT NULL,context TEXT NOT NULL,notified INTEGER DEFAULT 0,
                next_notify REAL DEFAULT 0,PRIMARY KEY(job,channel,context));
            CREATE INDEX IF NOT EXISTS action_subscribers_due ON action_subscribers(channel,notified,next_notify);
            CREATE TABLE IF NOT EXISTS action_requests(request_key TEXT PRIMARY KEY,job TEXT NOT NULL);
            INSERT OR IGNORE INTO action_subscribers(job,channel,context,notified,next_notify)
                SELECT id,channel,context,notified,next_notify FROM actions;
            INSERT OR IGNORE INTO action_requests SELECT request_key,id FROM actions;
        """,
        )
        if "adapter" not in {row[1] for row in self.db.execute("PRAGMA table_info(actions)")}:
            self.db.execute("ALTER TABLE actions ADD COLUMN adapter TEXT NOT NULL DEFAULT 'fail2ban'")

    def signal(self, node):
        return self.signals.setdefault(node, asyncio.Event())

    def capabilities(self, node):
        return self.store.state("capabilities:" + node, [])

    def adapter(self, node):
        return self.store.state("agent:" + node, {}).get("action_adapter", "fail2ban")

    @staticmethod
    def validate_results(results):
        if not isinstance(results, list) or len(results) > 20:
            raise ValueError("Invalid action results")
        for result in results:
            if (
                not isinstance(result, dict)
                or set(result) != {"id", "ok", "detail"}
                or not isinstance(result["id"], str)
                or not re.fullmatch("[a-f0-9]{32}", result["id"])
                or type(result["ok"]) is not bool
            ):
                raise ValueError("Invalid action result")
            if (
                not isinstance(result["detail"], str)
                or len(result["detail"]) > 160
                or any(ord(c) < 32 for c in result["detail"])
            ):
                raise ValueError("Invalid action detail")

    def request(self, node_id, action, ip, owner, request_key, channel, context):
        if action not in ("ban", "unban"):
            raise ValueError("Unknown action")
        ip = str(ipaddress.ip_address(ip))
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            previous = self.db.execute(
                "SELECT job FROM action_requests WHERE request_key=?", (request_key,)
            ).fetchone()
            if previous:
                return previous["job"]
            node = self.db.execute("SELECT * FROM nodes WHERE id=? AND enabled=1", (node_id,)).fetchone()
            now = time.time()
            if not node or self.store.node_status(node, now) in ("new", "offline"):
                raise ValueError("Node offline")
            if action not in self.capabilities(node_id):
                raise ValueError("Action unavailable")
            target = ActionTarget.command(node_id, self.adapter(node_id), ip)
            existing = self.latest(target)
            if existing and self.action_state(existing, now) in ("queued", "running"):
                if existing["action"] != action:
                    raise ValueError("Another action is pending")
                job_id = existing["id"]
            else:
                if (
                    existing
                    and self.action_state(existing, now) == "unknown"
                    and not self.reconciled(target, existing)
                ):
                    raise ValueError("Previous action is unconfirmed")
                pending = self.db.execute(
                    "SELECT COUNT(*) FROM actions WHERE node=? AND result IS NULL AND expires>?",
                    (node_id, now),
                ).fetchone()[0]
                if pending >= 5:
                    raise ValueError("Action queue busy")
                job_id = uuid.uuid4().hex
                self.db.execute(
                    "INSERT INTO actions(id,node,action,ip,requested_by,request_key,channel,context,created,expires,adapter) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        job_id,
                        node_id,
                        action,
                        ip,
                        owner,
                        request_key,
                        channel,
                        json.dumps(context, sort_keys=True),
                        now,
                        now + ACTION_TTL,
                        target.adapter,
                    ),
                )
            self.db.execute("INSERT INTO action_requests VALUES(?,?)", (request_key, job_id))
            self.db.execute(
                "INSERT OR IGNORE INTO action_subscribers(job,channel,context) VALUES(?,?,?)",
                (job_id, channel, json.dumps(context, sort_keys=True)),
            )
        self.signal(node_id).set()
        return job_id

    def latest(self, target):
        return self.db.execute(
            "SELECT * FROM actions WHERE node=? AND ip=? AND adapter=? ORDER BY created DESC,rowid DESC LIMIT 1",
            (target.node, target.ip, target.adapter),
        ).fetchone()

    def reconciled(self, target, job):
        return self.store.state(target.key, {}).get("time", 0) >= job["expires"]

    @staticmethod
    def action_state(job, now):
        if job["result"] is not None:
            result = json.loads(job["result"])
            if result["detail"] in UNCERTAIN_RESULTS:
                return "unknown"
            if result["detail"] != "Command expired":
                return "succeeded" if result["ok"] else "failed"
        elif now < job["expires"]:
            return "running" if job["leased"] else "queued"
        return "unknown" if job["leased"] else "expired"

    def fetch(self, node_id):
        now = time.time()
        with self.db:
            rows = self.db.execute(
                """SELECT id,action,ip,expires FROM actions WHERE node=? AND adapter=?
                   AND result IS NULL AND expires>? AND leased<=? ORDER BY created LIMIT 5""",
                (node_id, self.adapter(node_id), now, now),
            ).fetchall()
            for row in rows:
                self.db.execute("UPDATE actions SET leased=? WHERE id=?", (now + 60, row["id"]))
        return [dict(row) for row in rows]

    def accept(self, node_id, capabilities, results):
        self.store.set_state("capabilities:" + node_id, capabilities)
        accepted = []
        with self.db:
            for result in results:
                row = self.db.execute(
                    "SELECT * FROM actions WHERE id=? AND node=?", (result["id"], node_id)
                ).fetchone()
                if row:
                    if row["result"] is None or json.loads(row["result"])["detail"] == "Command expired":
                        self.db.execute(
                            "UPDATE actions SET result=? WHERE id=?", (json.dumps(result), result["id"])
                        )
                        self.db.execute(
                            "UPDATE action_subscribers SET notified=0,next_notify=0 WHERE job=?", (row["id"],)
                        )
                        if (
                            result["ok"]
                            and row["adapter"] == "fail2ban"
                            and self.adapter(node_id) == "fail2ban"
                            and result["detail"] != "Demo: firewall unchanged"
                        ):
                            target = ActionTarget.ssh(node_id, row["ip"])
                            self.store.record_access(
                                target, row["action"] == "ban", row["created"], observed_at=time.time()
                            )
                    accepted.append(result["id"])
        return accepted

    def due(self, channel):
        return self.db.execute(
            """SELECT a.*,s.channel AS subscriber_channel,s.context AS subscriber_context,
               s.rowid AS subscriber_id FROM action_subscribers s JOIN actions a ON a.id=s.job
               WHERE s.channel=? AND s.notified=0 AND s.next_notify<=?
               ORDER BY a.created,s.rowid LIMIT 1""",
            (channel, time.time()),
        ).fetchone()

    def done(self, job):
        with self.db:
            self.db.execute(
                "UPDATE action_subscribers SET notified=1 WHERE rowid=? AND (SELECT result FROM actions WHERE id=?) IS ?",
                (job["subscriber_id"], job["id"], job["result"]),
            )
            self.db.execute(
                """UPDATE actions SET notified=1 WHERE id=? AND NOT EXISTS
                   (SELECT 1 FROM action_subscribers WHERE job=? AND notified=0)""",
                (job["id"], job["id"]),
            )

    def failed(self, job, delay=30):
        with self.db:
            self.db.execute(
                "UPDATE action_subscribers SET next_notify=? WHERE rowid=?",
                (time.time() + delay, job["subscriber_id"]),
            )

    def cleanup(self):
        with self.db:
            expired = self.db.execute(
                "SELECT id FROM actions WHERE result IS NULL AND expires<?", (time.time(),)
            ).fetchall()
            for job in expired:
                self.db.execute(
                    "UPDATE actions SET result=? WHERE id=?",
                    (json.dumps({"id": job[0], "ok": False, "detail": "Command expired"}), job[0]),
                )
                self.db.execute(
                    "UPDATE action_subscribers SET notified=0,next_notify=0 WHERE job=?", (job[0],)
                )
            self.db.execute(
                """DELETE FROM actions WHERE notified=1 AND created<? AND NOT EXISTS
                   (SELECT 1 FROM action_subscribers WHERE job=actions.id AND notified=0)
                   AND NOT (leased>0 AND json_extract(result,'$.detail')='Command expired')""",
                (time.time() - 14 * 86400,),
            )
            self.db.execute("DELETE FROM action_subscribers WHERE job NOT IN (SELECT id FROM actions)")
            self.db.execute("DELETE FROM action_requests WHERE job NOT IN (SELECT id FROM actions)")
            self.db.execute(
                "DELETE FROM state WHERE key LIKE 'telegram:%:menu:%' AND json_extract(value,'$.created')<?",
                (time.time() - 14 * 86400,),
            )
