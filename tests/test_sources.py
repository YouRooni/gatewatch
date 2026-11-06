import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from gatewatch.sources.demo import DemoSource
from gatewatch.sources.file import FileSource


class SourceTest(unittest.IsolatedAsyncioTestCase):
    async def test_oversized_line_suffix_is_not_parsed(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "fail2ban.log"
            path.write_bytes(b"")
            source = FileSource({"path": str(path)})
            stream = source.records(None)
            cursor, _ = await anext(stream)
            await stream.aclose()
            path.write_bytes(b"x" * 65537 + b"[sshd] Ban 203.0.113.41\n[sshd] Ban 203.0.113.42\n")
            stream = source.records(cursor)
            _, events = await anext(stream)
            await stream.aclose()
            self.assertEqual([event.ip for event in events], ["203.0.113.42"])

    async def test_partial_line_waits_until_newline(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "fail2ban.log"
            path.write_bytes(b"")
            source = FileSource({"path": str(path), "poll_seconds": 1})
            stream = source.records(None)
            cursor, _ = await anext(stream)
            await stream.aclose()
            path.write_bytes(b"[sshd] Ban 203.0.113.42")
            stream = source.records(cursor)
            pending = asyncio.create_task(anext(stream))
            await asyncio.sleep(0.02)
            self.assertFalse(pending.done())
            with path.open("ab") as target:
                target.write(b"\n")
            _, events = await asyncio.wait_for(pending, 2)
            await stream.aclose()
            self.assertEqual(events[0].ip, "203.0.113.42")

    async def test_demo_has_stable_ids_and_runs_once(self):
        stream = DemoSource({}).records(None)
        cursor, first = await anext(stream)
        await stream.aclose()
        second_stream = DemoSource({}).records(None)
        _, second = await anext(second_stream)
        await second_stream.aclose()
        self.assertEqual([e.id for e in first], [e.id for e in second])
        self.assertEqual(len(first), 6)
        self.assertEqual({e.ip for e in first}, {"203.0.113.42"})
        resumed = DemoSource({}).records(cursor)
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(anext(resumed), 0.02)
        await resumed.aclose()

    async def test_file_starts_at_end_and_resumes_from_cursor(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "fail2ban.log"
            path.write_text("old data\n")
            source = FileSource({"path": str(path), "poll_seconds": 1})
            stream = source.records(None)
            cursor, events = await anext(stream)
            self.assertEqual(events, [])
            await stream.aclose()
            with path.open("a") as target:
                target.write("[sshd] Ban 203.0.113.42\n")
            resumed = source.records(cursor)
            cursor2, events = await anext(resumed)
            self.assertEqual(events[0].kind, "access.ban")
            self.assertGreater(json.loads(cursor2)["offset"], json.loads(cursor)["offset"])
            await resumed.aclose()
            reread = source.records(cursor)
            _, duplicate = await anext(reread)
            self.assertEqual(events[0].id, duplicate[0].id)
            await reread.aclose()

    async def test_file_rotation_restarts_available_file(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "fail2ban.log"
            path.write_text("old data\n")
            source = FileSource({"path": str(path), "poll_seconds": 1})
            stream = source.records(None)
            cursor, _ = await anext(stream)
            await stream.aclose()
            path.rename(Path(temp) / "old.log")
            path.write_text("[sshd] Unban 203.0.113.42\n")
            resumed = source.records(cursor)
            _, events = await anext(resumed)
            self.assertEqual(events[0].kind, "access.unban")
            await resumed.aclose()
