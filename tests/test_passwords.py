import time
import unittest

from gatewatch.model import Event
from gatewatch.render import render_rich
from gatewatch.sources.passwords import PasswordCapture


class PasswordTest(unittest.IsolatedAsyncioTestCase):
    def capture(self, **kwargs):
        capture = PasswordCapture("unused")
        item = {"pids": {123, 124}, "user": "root", "host": "203.0.113.42", "secret": bytearray(b"test-password"),
                "expires": time.monotonic() + 30, "captured": time.time(), **kwargs}
        capture.pending.append(item)
        return capture, item

    async def test_failed_password_gets_exact_match_and_secret_is_wiped(self):
        capture, item = self.capture()
        event = Event.new("auth.failure", "203.0.113.42", user="root", method="password")
        enriched = await capture.apply({"_PID": "124"}, event)
        self.assertEqual(enriched.password, "test-password")
        self.assertEqual(item["secret"], bytearray(len(b"test-password")))
        self.assertEqual(len(capture.pending), 0)
        await capture.close()

    async def test_successful_password_is_discarded(self):
        capture, item = self.capture()
        event = Event.new("auth.success", "203.0.113.42", user="root", method="password")
        enriched = await capture.apply({"_PID": "123"}, event)
        self.assertIsNone(enriched.password)
        self.assertNotIn("password", enriched.wire())
        self.assertFalse(any(item["secret"]))
        await capture.close()

    async def test_other_process_cannot_receive_password(self):
        capture, _ = self.capture()
        event = Event.new("auth.failure", "203.0.113.42", user="root", method="password")
        self.assertIsNone((await capture.apply({"_PID": "999"}, event)).password)
        self.assertEqual(len(capture.pending), 1)
        await capture.close()

    async def test_stale_log_cannot_take_recent_password(self):
        capture, _ = self.capture(captured=time.time() + 5)
        event = Event.new("auth.failure", "203.0.113.42", user="root", method="password")
        self.assertIsNone((await capture.apply({"_PID": "123"}, event)).password)
        await capture.close()

    async def test_expired_credentials_are_wiped(self):
        capture, item = self.capture(expires=time.monotonic() - 1)
        capture.trim()
        self.assertFalse(capture.pending)
        self.assertFalse(any(item["secret"]))

    async def test_openssh_placeholder_is_never_reported(self):
        capture, item = self.capture(secret=bytearray(b"\b\n\r\x7fINC"))
        event = Event.new("auth.failure", "203.0.113.42", user="root", method="password")
        self.assertIsNone((await capture.apply({"_PID": "123"}, event)).password)
        self.assertFalse(any(item["secret"]))

    async def test_invalid_user_password_is_not_claimed_as_original(self):
        capture, _ = self.capture()
        event = Event.new("auth.failure", "203.0.113.42", user="root", method="password")
        self.assertIsNone((await capture.apply({"_PID": "123", "MESSAGE": "Failed password for invalid user root from 203.0.113.42"}, event)).password)

    async def test_success_schema_forbids_password_field(self):
        event = Event.new("auth.success", "203.0.113.42", method="password").wire()
        with self.assertRaises(ValueError):
            Event.parse({**event, "password": "should-not-leave-agent"})

    async def test_html_in_password_is_escaped_and_controls_are_visible(self):
        card = {**Event.new("auth.failure", "203.0.113.42", user="root", method="password", password="<test>\n\0").wire(),
                "users": ["root"], "count": 1, "first": time.time(), "last": time.time()}
        body = render_rich({"name": "Node"}, card)
        self.assertIn("&lt;test&gt;\\n\\u0000", body)
        self.assertNotIn("<footer>", body)
        self.assertNotIn("Время события", body)
        card["last"] -= 120
        self.assertIn("Время события", render_rich({"name": "Node"}, card))
