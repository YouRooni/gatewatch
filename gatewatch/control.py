import asyncio
import ipaddress
import json
import re
import time
import uuid


class HubControl:
    def __init__(self, store):
        self.store, self.db = store, store.db
        self.signals = {}
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS actions(
                id TEXT PRIMARY KEY,node TEXT NOT NULL,action TEXT NOT NULL,ip TEXT NOT NULL,
                requested_by INTEGER NOT NULL,request_key TEXT UNIQUE NOT NULL,channel TEXT NOT NULL,
                context TEXT NOT NULL,created REAL NOT NULL,expires REAL NOT NULL,leased REAL DEFAULT 0,
                result TEXT,notified INTEGER DEFAULT 0,next_notify REAL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS actions_node ON actions(node,result,leased);
            CREATE INDEX IF NOT EXISTS actions_notify ON actions(channel,notified,next_notify);
        """)

    def signal(self, node):
        return self.signals.setdefault(node, asyncio.Event())

    def capabilities(self, node):
        return self.store.state('capabilities:' + node, [])

    @staticmethod
    def validate_results(results):
        if not isinstance(results, list) or len(results) > 20:
            raise ValueError("Invalid action results")
        for result in results:
            if not isinstance(result, dict) or set(result) != {"id", "ok", "detail"} or not isinstance(result["id"], str) or not re.fullmatch('[a-f0-9]{32}', result["id"]) or type(result["ok"]) is not bool:
                raise ValueError("Invalid action result")
            if not isinstance(result["detail"], str) or len(result["detail"]) > 160 or any(ord(c) < 32 for c in result["detail"]):
                raise ValueError("Invalid action detail")

    def request(self, node_id, action, ip, owner, request_key, channel, context):
        if action not in ("ban", "unban"):
            raise ValueError("Unknown action")
        ip = str(ipaddress.ip_address(ip))
        previous = self.db.execute('SELECT * FROM actions WHERE request_key=?', (request_key,)).fetchone()
        if previous:
            return previous['id']
        node = self.db.execute('SELECT * FROM nodes WHERE id=? AND enabled=1', (node_id,)).fetchone()
        if not node or time.time() - node['seen'] > 180:
            raise ValueError("Node offline")
        if action not in self.capabilities(node_id):
            raise ValueError("Action unavailable")
        if self.db.execute('SELECT COUNT(*) FROM actions WHERE node=? AND result IS NULL AND expires>?', (node_id,time.time())).fetchone()[0] >= 5:
            raise ValueError("Action queue busy")
        existing = self.db.execute('SELECT id FROM actions WHERE node=? AND action=? AND ip=? AND result IS NULL AND expires>?', (node_id,action,ip,time.time())).fetchone()
        if existing:
            return existing[0]
        job_id = uuid.uuid4().hex
        with self.db:
            self.db.execute('INSERT INTO actions(id,node,action,ip,requested_by,request_key,channel,context,created,expires) VALUES(?,?,?,?,?,?,?,?,?,?)',
                            (job_id,node_id,action,ip,owner,request_key,channel,json.dumps(context),time.time(),time.time()+180))
        self.signal(node_id).set()
        return job_id

    def fetch(self, node_id):
        now = time.time()
        with self.db:
            rows = self.db.execute('SELECT id,action,ip,expires FROM actions WHERE node=? AND result IS NULL AND expires>? AND leased<=? ORDER BY created LIMIT 5', (node_id,now,now)).fetchall()
            for row in rows:
                self.db.execute('UPDATE actions SET leased=? WHERE id=?', (now+60,row['id']))
        return [dict(row) for row in rows]

    def accept(self, node_id, capabilities, results):
        self.store.set_state('capabilities:' + node_id, capabilities)
        accepted = []
        with self.db:
            for result in results:
                row = self.db.execute('SELECT result FROM actions WHERE id=? AND node=?', (result['id'],node_id)).fetchone()
                if row:
                    if row['result'] is None:
                        self.db.execute('UPDATE actions SET result=? WHERE id=?', (json.dumps(result),result['id']))
                    accepted.append(result['id'])
        return accepted

    def due(self, channel):
        return self.db.execute('SELECT * FROM actions WHERE channel=? AND result IS NOT NULL AND notified=0 AND next_notify<=? ORDER BY created LIMIT 1', (channel,time.time())).fetchone()

    def done(self, job):
        with self.db:
            self.db.execute('UPDATE actions SET notified=1 WHERE id=?', (job['id'],))

    def failed(self, job):
        with self.db:
            self.db.execute('UPDATE actions SET next_notify=? WHERE id=?', (time.time()+30,job['id']))

    def cleanup(self):
        with self.db:
            expired = self.db.execute('SELECT id FROM actions WHERE result IS NULL AND expires<?', (time.time(),)).fetchall()
            for job in expired:
                self.db.execute('UPDATE actions SET result=? WHERE id=?', (json.dumps({'id':job[0],'ok':False,'detail':'Command expired'}),job[0]))
            self.db.execute('DELETE FROM actions WHERE notified=1 AND created<?', (time.time()-14*86400,))
            self.db.execute("DELETE FROM state WHERE key LIKE 'telegram:%:menu:%' AND json_extract(value,'$.created')<?", (time.time()-14*86400,))
