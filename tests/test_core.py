import json
import tempfile
import time
import unittest
from pathlib import Path

from gatewatch.agent import validate_endpoint
from gatewatch.cli import write_secret
from gatewatch.model import Event
from gatewatch.render import render
from gatewatch.sources.journal import parse_record
from gatewatch.storage import AgentStore, HubStore


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "hub.db")
        self.store = HubStore(self.path)
        self.node, self.token = self.store.add_node("Main-X")

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def event(self, **kwargs):
        return Event.new("auth.failure", "2001:db8::1", user="root", **kwargs)

    def test_authentication_and_revocation(self):
        self.assertIsNotNone(self.store.authenticate(self.node, self.token))
        self.assertIsNone(self.store.authenticate(self.node, "wrong"))
        with self.store.db:
            self.store.db.execute("UPDATE nodes SET enabled=0")
        self.assertIsNone(self.store.authenticate(self.node, self.token))

    def test_source_failure_and_recovery_are_reported_once(self):
        self.store.ingest(self.node, [], ["tg"])
        self.store.health(["tg"], 180)
        self.store.set_state("agent:" + self.node, {"sources": {"journal": "error"}})
        self.store.health(["tg"], 180)
        self.store.health(["tg"], 180)
        self.store.set_state("agent:" + self.node, {"sources": {"journal": "ok"}})
        self.store.health(["tg"], 180)
        kinds = [row[0] for row in self.store.db.execute("SELECT kind FROM cards ORDER BY rowid")]
        self.assertEqual(kinds, ["node.online", "node.degraded", "node.recovered"])

    def test_dedup_survives_restart(self):
        event = self.event()
        self.store.ingest(self.node, [event, event], ["tg"])
        self.store.db.close()
        self.store = HubStore(self.path)
        self.store.ingest(self.node, [event], ["tg"])
        card = self.store.due("tg")
        self.assertEqual(json.loads(card["payload"])["count"], 1)

    def test_same_event_id_is_scoped_to_node(self):
        event = self.event()
        second, _ = self.store.add_node("Node-2")
        self.store.ingest(self.node, [event], ["tg"])
        self.store.ingest(second, [event], ["tg"])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 2)

    def test_ack_during_update_keeps_new_version_pending(self):
        event = self.event()
        self.store.ingest(self.node, [event], ["tg", "other"])
        before = self.store.due("tg")
        self.store.ingest(self.node, [self.event()], ["tg", "other"])
        self.store.done(before, "tg", {"message": 1})
        row = self.store.db.execute("SELECT version FROM cards").fetchone()
        delivery = self.store.db.execute("SELECT version FROM deliveries WHERE channel='tg'").fetchone()
        self.assertGreater(row[0], delivery[0])
        self.assertIsNotNone(self.store.due("other"))

    def test_queue_limit_backpressures_without_dedup_loss(self):
        self.store.max_pending = 1
        first, second = self.event(), self.event()
        self.store.ingest(self.node, [first], ["tg"])
        with self.assertRaises(OverflowError):
            self.store.ingest(self.node, [second], ["tg"])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)
        self.store.ingest(self.node, [first], ["tg"])

    def test_subscription_filters_success_and_can_enable_it(self):
        event = Event.new("auth.success", "203.0.113.42")
        self.store.ingest(self.node, [event], {"quiet": {"auth.failure"}, "audit": {"auth.success"}})
        self.assertIsNone(self.store.due("quiet"))
        self.assertIsNotNone(self.store.due("audit"))

    def test_services_do_not_merge(self):
        self.store.ingest(self.node, [self.event(service="ssh"), self.event(service="vpn")], ["tg"])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM cards").fetchone()[0], 2)

    def test_dedup_at_record_capacity_is_still_acknowledged(self):
        self.store.max_events = 1
        event = self.event()
        self.store.ingest(self.node, [event], ["tg"])
        self.store.ingest(self.node, [event], ["tg"])
        with self.assertRaises(OverflowError):
            self.store.ingest(self.node, [self.event()], ["tg"])

    def test_success_and_ban_are_individual_cards(self):
        events = [
            self.event(),
            Event.new("auth.success", "2001:db8::1"),
            Event.new("access.ban", "2001:db8::1"),
        ]
        self.store.ingest(self.node, events, ["tg"])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM cards").fetchone()[0], 3)
        self.assertNotEqual(self.store.due("tg")["kind"], "auth.failure")

    def test_health_transitions_once(self):
        self.store.ingest(self.node, [], ["tg"])
        self.store.health(["tg"], 180)
        self.store.health(["tg"], 180)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM cards").fetchone()[0], 1)
        with self.store.db:
            self.store.db.execute("UPDATE nodes SET seen=?", (time.time() - 200,))
        self.store.health(["tg"], 180)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM cards").fetchone()[0], 2)

    def test_agent_cursor_and_queue_are_atomic(self):
        agent = AgentStore(str(Path(self.temp.name) / "agent.db"), limit=1)
        try:
            first, second = self.event(), self.event()
            agent.append("journal", "a", [first])
            with self.assertRaises(OverflowError):
                agent.append("journal", "b", [second])
            self.assertEqual(agent.cursor("journal"), "a")
            batch = agent.batch()
            self.assertEqual(batch[0]["id"], first.id)
            agent.ack(batch)
            agent.append("journal", "b", [second])
            self.assertEqual(agent.cursor("journal"), "b")
        finally:
            agent.db.close()

    def test_agent_batches_respect_byte_limit(self):
        agent = AgentStore(str(Path(self.temp.name) / "large.db"))
        try:
            events = [
                Event.new("auth.failure", "203.0.113.42", method="password", password="\U0001f600" * 512)
                for _ in range(100)
            ]
            agent.append("test", "one", events)
            batch = agent.batch()
            self.assertLess(len(batch), 100)
            self.assertLess(
                len(json.dumps({"version": 1, "node_id": self.node, "events": batch}).encode()), 128 * 1024
            )
            agent.ack(batch)
            self.assertTrue(agent.has_pending())
        finally:
            agent.db.close()


class ParsingTest(unittest.TestCase):
    def record(self, message, identity="sshd"):
        return {
            "MESSAGE": message,
            "SYSLOG_IDENTIFIER": identity,
            "__CURSOR": "s=test;i=42",
            "__REALTIME_TIMESTAMP": str(int(time.time() * 1e6)),
        }

    def test_ssh_ipv4_ipv6_and_methods(self):
        for state, method, address in (
            ("Failed", "password", "198.51.100.1"),
            ("Accepted", "publickey", "2001:db8::1"),
            ("Failed", "keyboard-interactive/pam", "::1"),
        ):
            event = parse_record(
                self.record(f"{state} {method} for invalid user admin from {address} port 4422 ssh2")
            )[0]
            self.assertEqual(event.ip, address)
            self.assertEqual(event.user, "admin")
            self.assertEqual(event.method, method)
            self.assertEqual(event.port, 4422)

    def test_no_duplicate_invalid_user(self):
        self.assertEqual(parse_record(self.record("Invalid user admin from 198.51.100.1 port 2222")), [])

    def test_no_password_or_raw_journal_leaves_parser(self):
        record = self.record("Failed password for root from 198.51.100.1 port 22 ssh2: SECRET")
        self.assertNotIn("SECRET", json.dumps(parse_record(record)[0].wire()))
        self.assertNotIn("MESSAGE", parse_record(record)[0].wire())

    def test_fail2ban_and_unban(self):
        for action in ("Ban", "Unban"):
            event = parse_record(
                self.record(
                    f"2026 fail2ban.actions [123]: NOTICE [sshd] {action} 2001:db8::1", "fail2ban-server"
                )
            )[0]
            self.assertEqual(event.kind, "access." + action.lower())

    def test_spoofed_unrelated_unit_ignored(self):
        self.assertEqual(
            parse_record(self.record("Accepted password for root from 1.1.1.1 port 22", "app")), []
        )

    def test_schema_rejects_invalid_and_raw_fields(self):
        event = Event.new("auth.failure", "1.1.1.1").wire()
        for field, value in (
            ("port", True),
            ("ip", "not-ip"),
            ("time", float("nan")),
            ("user", "x\ny"),
            ("password", "secret"),
            ("time", time.time() - 8 * 86400),
        ):
            with self.subTest(field=field, value=value), self.assertRaises((ValueError, TypeError)):
                Event.parse({**event, field: value})

    def test_endpoint_does_not_leak_tokens_to_http_or_redirects(self):
        self.assertEqual(validate_endpoint("http://127.0.0.1:8787/"), "http://127.0.0.1:8787")
        for endpoint in (
            "http://public.example",
            "https://u:p@public.example",
            "https://public.example?secret=1",
            "file:///tmp/a",
        ):
            with self.assertRaises(ValueError):
                validate_endpoint(endpoint)

    def test_html_escape_and_timezone(self):
        card = {
            **Event.new("auth.failure", "1.1.1.1", method="password").wire(),
            "users": ["<root>&"],
            "count": 3,
            "first": time.time(),
            "last": time.time(),
        }
        body = render({"name": "<Main>"}, card, timezone="Asia/Novosibirsk", custom=True)
        self.assertIn("&lt;root&gt;&amp;", body)
        self.assertIn("5253864872780769235", body)
        self.assertNotIn("<Main>", body)
        self.assertNotIn("&lt;Main&gt;", body)
        self.assertIn("&lt;Main&gt;", render({"name": "<Main>"}, card, show_node_name=True))

    def test_location_uses_custom_flag_with_unicode_fallback(self):
        from gatewatch.flags import country_flag
        from gatewatch.render import render_rich

        card = {
            **Event.new("auth.failure", "1.1.1.1").wire(),
            "users": ["root"],
            "count": 1,
            "first": time.time(),
            "last": time.time(),
        }
        body = render_rich(
            {"name": "Node"}, card, geo="Indonesia · Jakarta · AS4787 Example", country_code="ID", custom=True
        )
        self.assertIn("🇮🇩", body)
        self.assertIn("<b>Локация:</b>", body)
        self.assertEqual(country_flag("DE"), "🇩🇪")
        self.assertIn("5305515672741103725", country_flag("DE", True))

    def test_secret_files_never_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "agent.secret.json")
            write_secret(path, {"token": "one"})
            with self.assertRaises(FileExistsError):
                write_secret(path, {"token": "two"})
            self.assertEqual(json.loads(Path(path).read_text())["token"], "one")
