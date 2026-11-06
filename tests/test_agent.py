import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestServer

from gatewatch.agent import run
from gatewatch.model import Event


class AgentTest(unittest.IsolatedAsyncioTestCase):
    async def test_transient_error_retries_same_event_then_acknowledges(self):
        event = Event.new("auth.success", "203.0.113.42")
        received = []
        delivered = asyncio.Event()

        class Source:
            def __init__(self, options):
                pass

            async def records(self, cursor):
                if not cursor:
                    yield "one", [event]
                while True:
                    await asyncio.sleep(100)

        async def accept(request):
            payload = await request.json()
            if payload["events"]:
                received.append(payload["events"])
                if len(received) == 1:
                    return web.Response(status=503)
                delivered.set()
            return web.json_response({"accepted": len(payload["events"])})

        app = web.Application()
        app.router.add_post("/v1/events", accept)
        server = TestServer(app)
        await server.start_server()
        with tempfile.TemporaryDirectory() as temp:
            database = str(Path(temp) / "agent.db")
            config = {
                "database": database,
                "endpoint": str(server.make_url("/")),
                "node_id": "fake-node",
                "token": "fake-token",
                "sources": {"test": {"type": "test"}},
            }
            with patch("gatewatch.agent.plugin", return_value=Source):
                task = asyncio.create_task(run(config))
                try:
                    await asyncio.wait_for(delivered.wait(), 8)
                    await asyncio.sleep(0.05)
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            self.assertEqual(len(received), 2)
            self.assertEqual(received[0], received[1])
            db = sqlite3.connect(database)
            try:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM queue").fetchone()[0], 0)
                self.assertEqual(db.execute("SELECT cursor FROM cursors").fetchone()[0], "one")
            finally:
                db.close()
        await server.close()
