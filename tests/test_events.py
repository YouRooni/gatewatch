import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gatewatch.cli import dispatch, parser
from gatewatch.config import DEFAULT_EVENTS
from gatewatch.model import Event
from gatewatch.render import render, render_rich
from gatewatch.storage import AgentStore, HubStore


class ServiceEventTest(unittest.TestCase):
    def event(self, **values):
        return Event.new(
            "event", title="Backup <failed>", service="backup", level="error", detail="disk <full>", **values
        )

    def test_optional_address_and_reserved_auth_fields(self):
        event = self.event()
        self.assertEqual(Event.parse(event.wire()), event)
        for change in (
            {"level": "critical"},
            {"title": ""},
            {"ip": "bad"},
            {"password": "secret"},
            {"user": "root"},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                Event.parse({**event.wire(), **change})
        with self.assertRaises(ValueError):
            Event.parse(Event.new("auth.failure").wire())

    def test_render_escapes_content_and_keeps_description_visible(self):
        card = {**self.event().wire(), "count": 1, "first": 1, "last": 1}
        for renderer in (render, render_rich):
            html = renderer({"name": "Test"}, card)
            self.assertIn("Backup &lt;failed&gt;", html)
            self.assertIn("disk &lt;full&gt;", html.split("<details>")[0])
            self.assertNotIn("<failed>", html)

    def test_events_are_durable_deduplicated_and_subscribed(self):
        with tempfile.TemporaryDirectory() as temp:
            store = HubStore(str(Path(temp) / "hub.db"))
            node, _ = store.add_node("Test")
            event = self.event()
            store.ingest(node, [event, event], {"all": DEFAULT_EVENTS, "ssh": {"auth.failure"}})
            self.assertEqual(store.db.execute("SELECT count(*) FROM cards").fetchone()[0], 1)
            self.assertIsNotNone(store.due("all"))
            self.assertIsNone(store.due("ssh"))
            store.db.close()

    def test_emit_queues_locally_with_stable_id(self):
        with tempfile.TemporaryDirectory() as temp:
            database = str(Path(temp) / "agent.db")
            config = {"role": "agent", "database": database}
            args = parser().parse_args(["emit", "Backup complete", "--service", "backup", "--id", "a" * 32])
            with patch("gatewatch.cli.read_config", return_value=config), patch("builtins.print"):
                dispatch(args)
                dispatch(args)
            store = AgentStore(database)
            self.assertEqual(len(store.batch()), 1)
            self.assertEqual(store.batch()[0]["title"], "Backup complete")
            self.assertEqual(
                json.loads(store.db.execute("SELECT payload FROM queue").fetchone()[0])["ip"], ""
            )
            store.db.close()
