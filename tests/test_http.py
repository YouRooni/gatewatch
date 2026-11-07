import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

from gatewatch.hub import create_app
from gatewatch.model import Event
from gatewatch.storage import HubStore


class RecordingChannel:
    def __init__(self, config, store, channel_id):
        self.messages = []

    async def deliver(self, node, card, receipt):
        self.messages.append((node["id"], card))
        return {"id": len(self.messages)}

    async def close(self):
        pass


class HTTPTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = HubStore(str(Path(self.temp.name) / "hub.db"))
        self.node, self.token = self.store.add_node("Test")
        with patch("gatewatch.hub.plugin", return_value=RecordingChannel):
            app = create_app({"database": "unused", "channels": {"test": {"type": "recording"}}}, self.store)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.temp.cleanup()

    async def post(self, events, token=None):
        return await self.client.post(
            "/v1/events",
            json={"version": 1, "node_id": self.node, "events": events},
            headers={"Authorization": "Bearer " + (token or self.token)},
        )

    async def test_batch_is_atomic_and_unknown_fields_rejected(self):
        event = Event.new("auth.failure", "1.1.1.1").wire()
        response = await self.post([event, {**event, "password": "secret"}])
        self.assertEqual(response.status, 400)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)

    async def test_wrong_token_and_revoked_node(self):
        self.assertEqual((await self.post([], "wrong")).status, 401)
        with self.store.db:
            self.store.db.execute("UPDATE nodes SET enabled=0")
        self.assertEqual((await self.post([])).status, 401)

    async def test_heartbeat_and_retries(self):
        self.assertEqual((await self.post([])).status, 200)
        event = Event.new("auth.success", "1.1.1.1").wire()
        for _ in range(2):
            response = await self.post([event])
            self.assertEqual(await response.json(), {"accepted": 1})
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)
        await asyncio.sleep(0.05)
        pending = self.store.db.execute(
            "SELECT COUNT(*) FROM deliveries d JOIN cards c ON c.id=d.card WHERE d.version<c.version"
        ).fetchone()[0]
        self.assertEqual(pending, 0)

    async def test_oversized_batch_and_body(self):
        event = Event.new("auth.failure", "1.1.1.1").wire()
        self.assertEqual((await self.post([event] * 101)).status, 400)
        response = await self.client.post(
            "/v1/events", data="x" * (129 * 1024), headers={"Authorization": "Bearer " + self.token}
        )
        self.assertEqual(response.status, 413)

    async def test_protocol_version(self):
        response = await self.client.post(
            "/v1/events",
            json={"version": 2, "node_id": self.node, "events": []},
            headers={"Authorization": "Bearer " + self.token},
        )
        self.assertEqual(response.status, 400)

    async def test_expiration_error_identifies_only_the_expired_event(self):
        fresh = Event.new("auth.failure", "198.51.100.2").wire()
        old = {**fresh, "id": "a" * 32, "time": time.time() - 7 * 86400 - 1}
        response = await self.post([old, fresh])
        self.assertEqual(response.status, 400)
        self.assertEqual(await response.json(), {"error": "events_expired", "ids": [old["id"]]})
        response = await self.post([fresh])
        self.assertEqual(await response.json(), {"accepted": 1})

    async def test_probe_checks_identity_without_heartbeat(self):
        response = await self.client.get(
            "/v1/probe", params={"node_id": self.node}, headers={"Authorization": "Bearer " + self.token}
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(self.store.db.execute("SELECT seen FROM nodes").fetchone()[0], 0)
        response = await self.client.get("/readyz")
        self.assertEqual(await response.json(), {"status": "ready"})

    async def test_custom_event_without_ip_is_accepted_and_delivered(self):
        event = Event.new("event", title="Backup complete", service="backup", level="info").wire()
        response = await self.post([event])
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["accepted"], 1)
        await asyncio.sleep(0.05)
        self.assertEqual(self.store.db.execute("SELECT kind FROM cards").fetchone()[0], "event")
