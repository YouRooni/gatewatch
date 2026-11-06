import asyncio
import tempfile
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
