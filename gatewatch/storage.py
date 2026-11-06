import hashlib
import json
import secrets
import time
import uuid

from .database import connect, schema, script


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


class HubStore:
    def __init__(self, path, window=300, max_pending=100000, max_events=1000000):
        self.db = connect(path, "hub")
        self.window = window
        self.max_pending = max_pending
        self.max_events = max_events
        with schema(self.db, "hub"):
            script(
                self.db,
                """
                CREATE TABLE IF NOT EXISTS nodes(
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, token TEXT UNIQUE NOT NULL,
                    seen REAL DEFAULT 0, health TEXT DEFAULT 'new', muted REAL DEFAULT 0,
                    enabled INTEGER DEFAULT 1, address TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, received REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS cards(
                    id TEXT PRIMARY KEY, node TEXT NOT NULL, kind TEXT NOT NULL,
                    payload TEXT NOT NULL, version INTEGER NOT NULL, created REAL NOT NULL, ip TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS deliveries(
                    card TEXT NOT NULL, channel TEXT NOT NULL, version INTEGER DEFAULT 0,
                    receipt TEXT, next_try REAL DEFAULT 0, failures INTEGER DEFAULT 0,
                    dirty INTEGER DEFAULT 1,
                    PRIMARY KEY(card,channel));
                CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS counters(key TEXT PRIMARY KEY,value INTEGER NOT NULL);
                INSERT OR IGNORE INTO counters SELECT 'events',COUNT(*) FROM events;
                CREATE TRIGGER IF NOT EXISTS events_insert AFTER INSERT ON events BEGIN
                    UPDATE counters SET value=value+1 WHERE key='events'; END;
                CREATE TRIGGER IF NOT EXISTS events_delete AFTER DELETE ON events BEGIN
                    UPDATE counters SET value=value-1 WHERE key='events'; END;
                CREATE INDEX IF NOT EXISTS deliveries_due ON deliveries(next_try,version);
                CREATE INDEX IF NOT EXISTS cards_created ON cards(created);
                CREATE INDEX IF NOT EXISTS events_received ON events(received);
            """,
            )
            if "dirty" not in {row[1] for row in self.db.execute("PRAGMA table_info(deliveries)")}:
                self.db.execute("ALTER TABLE deliveries ADD COLUMN dirty INTEGER DEFAULT 1")
                self.db.execute(
                    "UPDATE deliveries SET dirty=0 WHERE version=(SELECT version FROM cards WHERE id=deliveries.card)"
                )
            if "address" not in {row[1] for row in self.db.execute("PRAGMA table_info(nodes)")}:
                self.db.execute("ALTER TABLE nodes ADD COLUMN address TEXT NOT NULL DEFAULT ''")
            self.db.execute("CREATE INDEX IF NOT EXISTS deliveries_dirty ON deliveries(dirty,next_try)")
            if "ip" not in {row[1] for row in self.db.execute("PRAGMA table_info(cards)")}:
                self.db.execute("ALTER TABLE cards ADD COLUMN ip TEXT NOT NULL DEFAULT ''")
                self.db.execute("UPDATE cards SET ip=COALESCE(json_extract(payload,'$.ip'),'')")
            self.db.execute("CREATE INDEX IF NOT EXISTS cards_flow ON cards(node,ip,kind)")
            from .control import HubControl

            self.control = HubControl(self)

    def add_node(self, name, address=""):
        node_id, token = str(uuid.uuid4()), secrets.token_urlsafe(32)
        with self.db:
            self.db.execute(
                "INSERT INTO nodes(id,name,token,address) VALUES(?,?,?,?)",
                (node_id, name, digest(token), address),
            )
        return node_id, token

    def authenticate(self, node_id, token):
        row = self.db.execute("SELECT * FROM nodes WHERE id=? AND enabled=1", (node_id,)).fetchone()
        return row if row and secrets.compare_digest(row["token"], digest(token)) else None

    def state(self, key, default=None):
        row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_state(self, key, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO state VALUES(?,?)", (key, json.dumps(value)))

    def ingest(self, node_id, events, channels):
        now = time.time()
        candidates = {node_id + ":" + event.id for event in events}
        known = set()
        if candidates:
            marks = ",".join("?" for _ in candidates)
            known = {
                row[0]
                for row in self.db.execute(f"SELECT id FROM events WHERE id IN ({marks})", list(candidates))
            }
        new_count = len(candidates - known)
        pending = self.db.execute("SELECT COUNT(*) FROM deliveries WHERE dirty=1").fetchone()[0]
        if pending >= self.max_pending and new_count:
            raise OverflowError("Queue full")
        if (
            new_count
            and self.db.execute("SELECT value FROM counters WHERE key='events'").fetchone()[0] + new_count
            > self.max_events
        ):
            raise OverflowError("Deduplication capacity reached")
        with self.db:
            self.db.execute("UPDATE nodes SET seen=? WHERE id=?", (now, node_id))
            for event in events:
                inserted = self.db.execute(
                    "INSERT OR IGNORE INTO events VALUES(?,?)", (node_id + ":" + event.id, now)
                ).rowcount
                if not inserted:
                    continue
                if event.kind in ("access.ban", "access.unban"):
                    state_key = f"blocked:{node_id}:{event.ip}"
                    previous = self.state(state_key, {})
                    if event.time >= previous.get("time", 0):
                        self.db.execute(
                            "INSERT OR REPLACE INTO state VALUES(?,?)",
                            (
                                state_key,
                                json.dumps({"blocked": event.kind == "access.ban", "time": event.time}),
                            ),
                        )
                group = hashlib.sha256(
                    json.dumps([event.service, event.ip, int(event.time // self.window)]).encode()
                ).hexdigest()
                key = (
                    f"{node_id}:failure:{group}" if event.kind == "auth.failure" else f"{node_id}:{event.id}"
                )
                row = self.db.execute("SELECT payload FROM cards WHERE id=?", (key,)).fetchone()
                if row:
                    payload = json.loads(row[0])
                    if payload.get("password") is not None and not payload.get("passwords"):
                        payload["passwords"] = [{"value": payload["password"], "count": 1}]
                    if event.time >= payload["last"]:
                        payload["password"] = event.password
                        payload["port"] = event.port
                        payload["method"] = event.method
                    payload["count"] += 1
                    payload["first"] = min(payload["first"], event.time)
                    payload["last"] = max(payload["last"], event.time)
                    payload["users"] = sorted(set(payload["users"] + [event.user]))[:8]
                else:
                    payload = {
                        **event.wire(),
                        "count": 1,
                        "first": event.time,
                        "last": event.time,
                        "users": [event.user],
                    }
                if event.password is not None:
                    samples = payload.get("passwords", [])
                    sample = next((sample for sample in samples if sample["value"] == event.password), None)
                    if sample:
                        samples.remove(sample)
                        sample["count"] += 1
                    else:
                        sample = {"value": event.password, "count": 1}
                    payload["passwords"] = [sample, *samples][:8]
                self._card(key, node_id, event.kind, payload, channels, now)

    def _card(self, key, node, kind, payload, channels, now):
        self.db.execute(
            """INSERT INTO cards(id,node,kind,payload,version,created,ip) VALUES(?,?,?,?,1,?,?) ON CONFLICT(id) DO UPDATE
                           SET payload=excluded.payload, version=cards.version+1""",
            (key, node, kind, json.dumps(payload), now, payload.get("ip", "")),
        )
        for channel in channels:
            if isinstance(channels, dict) and kind not in channels[channel]:
                continue
            self.db.execute(
                "INSERT OR IGNORE INTO deliveries(card,channel,next_try) VALUES(?,?,?)",
                (key, channel, now + 5 if kind == "access.ban" else 0),
            )
        self.db.execute("UPDATE deliveries SET dirty=1 WHERE card=?", (key,))

    def health(self, channels, timeout):
        now = time.time()
        with self.db:
            for node in self.db.execute("SELECT * FROM nodes WHERE enabled=1 AND seen>0").fetchall():
                sources = self.state("agent:" + node["id"], {}).get("sources", {})
                errors = [source for source, status in sources.items() if status == "error"]
                status = "offline" if now - node["seen"] > timeout else "degraded" if errors else "online"
                if status == node["health"]:
                    continue
                self.db.execute("UPDATE nodes SET health=? WHERE id=?", (status, node["id"]))
                kind = (
                    "node.recovered"
                    if node["health"] == "degraded" and status == "online"
                    else "node." + status
                )
                payload = {"kind": kind, "first": now, "last": now, "count": 1, "sources": errors}
                self._card(str(uuid.uuid4()), node["id"], payload["kind"], payload, channels, now)

    def due(self, channel):
        return self.db.execute(
            """SELECT c.*,d.receipt,d.failures FROM deliveries d JOIN cards c ON c.id=d.card
            JOIN nodes n ON n.id=c.node WHERE d.channel=? AND d.dirty=1 AND d.next_try<=?
            AND n.enabled=1 AND (c.kind!='access.ban' OR NOT EXISTS (
                SELECT 1 FROM cards f JOIN deliveries fd ON fd.card=f.id
                WHERE f.node=c.node AND f.ip=c.ip AND f.kind='auth.failure' AND fd.channel=d.channel AND fd.dirty=1))
            AND (c.kind!='access.unban' OR NOT EXISTS (
                SELECT 1 FROM cards b JOIN deliveries bd ON bd.card=b.id
                WHERE b.node=c.node AND b.ip=c.ip AND b.kind='access.ban' AND bd.channel=d.channel AND bd.dirty=1
                AND (b.created<c.created OR (b.created=c.created AND b.rowid<c.rowid))))
            ORDER BY CASE WHEN c.kind='auth.failure' THEN 1 ELSE 0 END,c.created,c.rowid LIMIT 1""",
            (channel, time.time()),
        ).fetchone()

    def done(self, card, channel, receipt):
        with self.db:
            self.db.execute(
                """UPDATE deliveries SET version=?,receipt=?,next_try=?,failures=0,
                dirty=CASE WHEN (SELECT version FROM cards WHERE id=?)>? THEN 1 ELSE 0 END WHERE card=? AND channel=?""",
                (
                    card["version"],
                    json.dumps(receipt),
                    time.time() + 15,
                    card["id"],
                    card["version"],
                    card["id"],
                    channel,
                ),
            )

    def next_due(self, channel):
        row = self.db.execute(
            "SELECT MIN(d.next_try) FROM deliveries d JOIN cards c ON c.id=d.card JOIN nodes n ON n.id=c.node WHERE d.channel=? AND d.dirty=1 AND d.next_try>? AND n.enabled=1",
            (channel, time.time()),
        ).fetchone()
        return row[0] if row else None

    def failed(self, card, channel, delay):
        with self.db:
            self.db.execute(
                "UPDATE deliveries SET failures=failures+1,next_try=? WHERE card=? AND channel=?",
                (time.time() + delay, card["id"], channel),
            )

    def cleanup(self):
        self.control.cleanup()
        cutoff = time.time() - 14 * 86400
        with self.db:
            self.db.execute("DELETE FROM events WHERE received<?", (cutoff,))
            self.db.execute(
                """DELETE FROM cards WHERE created<? AND NOT EXISTS
                (SELECT 1 FROM deliveries d WHERE d.card=cards.id AND d.version<cards.version)""",
                (cutoff,),
            )
            self.db.execute("DELETE FROM deliveries WHERE card NOT IN (SELECT id FROM cards)")
        self.db.execute("PRAGMA wal_checkpoint(PASSIVE)")


class AgentStore:
    def __init__(self, path, limit=50000):
        self.db = connect(path, "agent")
        self.db.execute("PRAGMA secure_delete=ON")
        self.limit = limit
        with schema(self.db, "agent"):
            script(
                self.db,
                """
                CREATE TABLE IF NOT EXISTS queue(id TEXT PRIMARY KEY,payload TEXT NOT NULL,created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS cursors(source TEXT PRIMARY KEY,cursor TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS action_results(id TEXT PRIMARY KEY,result TEXT NOT NULL,acked INTEGER DEFAULT 0,created REAL NOT NULL);
            """,
            )

    def cursor(self, source):
        row = self.db.execute("SELECT cursor FROM cursors WHERE source=?", (source,)).fetchone()
        return row[0] if row else None

    def append(self, source, cursor, events):
        with self.db:
            self.db.execute("DELETE FROM queue WHERE created<?", (time.time() - 7 * 86400 + 600,))
            if self.db.execute("SELECT COUNT(*) FROM queue").fetchone()[0] + len(events) > self.limit:
                raise OverflowError("Agent queue full; journal cursor retained")
            for event in events:
                self.db.execute(
                    "INSERT OR IGNORE INTO queue VALUES(?,?,?)",
                    (event.id, json.dumps(event.wire()), time.time()),
                )
            if source is not None:
                self.db.execute("INSERT OR REPLACE INTO cursors VALUES(?,?)", (source, cursor))

    def batch(self, max_bytes=120 * 1024):
        events, size = [], 0
        for row in self.db.execute("SELECT payload FROM queue ORDER BY created LIMIT 100"):
            length = len(row[0].encode("utf-8")) + 1
            if events and size + length > max_bytes:
                break
            events.append(json.loads(row[0]))
            size += length
        return events

    def has_pending(self):
        return self.db.execute("SELECT 1 FROM queue LIMIT 1").fetchone() is not None

    def ack(self, events):
        with self.db:
            self.db.executemany("DELETE FROM queue WHERE id=?", [(e["id"],) for e in events])

    def action_result(self, job_id):
        row = self.db.execute("SELECT result FROM action_results WHERE id=?", (job_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_action(self, job_id, result):
        result = {"id": job_id, "ok": bool(result["ok"]), "detail": str(result["detail"])[:160]}
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO action_results(id,result,created) VALUES(?,?,?)",
                (job_id, json.dumps(result), time.time()),
            )
            self.db.execute("UPDATE action_results SET acked=0 WHERE id=?", (job_id,))
            self.db.execute("DELETE FROM action_results WHERE created<?", (time.time() - 14 * 86400,))

    def action_results(self):
        return [
            json.loads(row[0])
            for row in self.db.execute(
                "SELECT result FROM action_results WHERE acked=0 ORDER BY created LIMIT 20"
            )
        ]

    def ack_actions(self, ids):
        with self.db:
            self.db.executemany("UPDATE action_results SET acked=1 WHERE id=?", [(job_id,) for job_id in ids])
