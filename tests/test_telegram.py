import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from gatewatch.channels.telegram import TelegramChannel, TelegramError
from gatewatch.model import Event
from gatewatch.storage import HubStore


class TelegramTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = HubStore(str(Path(self.temp.name) / "hub.db"))
        node, _ = self.store.add_node("Main-X")
        self.node = dict(self.store.db.execute("SELECT * FROM nodes WHERE id=?", (node,)).fetchone())
        self.channel = TelegramChannel({"token": "123:fake", "chat_id": -100123, "owner_ids": [42]}, self.store, "tg")
        self.store.set_state(self.channel.topic_key(node), {"id": 11, "name": "My custom topic"})
        self.card = {**Event.new("auth.failure", "1.1.1.1").wire(), "users": ["root"], "count": 1, "first": 1, "last": 1}

    async def asyncTearDown(self):
        await self.channel.close()
        self.store.db.close()
        self.temp.cleanup()

    async def test_deleted_topic_recreated_with_user_name(self):
        self.channel.call = AsyncMock(side_effect=[TelegramError("Bad Request: message thread not found"),
                                                   {"message_thread_id": 22, "name": "My custom topic"}, {"message_id": 50}])
        receipt = await self.channel.deliver(self.node, self.card, None)
        self.assertEqual(receipt, {"topic": 22, "message": 50})
        self.assertEqual(self.channel.call.call_args_list[1].kwargs["name"], "My custom topic")

    async def test_closed_topic_not_recreated(self):
        self.channel.call = AsyncMock(side_effect=TelegramError("Bad Request: TOPIC_CLOSED"))
        with self.assertRaises(TelegramError):
            await self.channel.deliver(self.node, self.card, None)
        self.assertEqual(self.channel.call.call_count, 1)

    async def test_deleted_message_is_sent_again(self):
        self.channel.call = AsyncMock(side_effect=[TelegramError("message to edit not found"), {"message_id": 52}])
        receipt = await self.channel.deliver(self.node, self.card, {"topic": 11, "message": 3})
        self.assertEqual(receipt["message"], 52)
        self.assertEqual(self.channel.call.call_args_list[1].args[0], "sendMessage")

    async def test_custom_emoji_fallback(self):
        self.channel.call = AsyncMock(side_effect=[TelegramError("custom emoji not allowed"), {"message_id": 52}])
        await self.channel.deliver(self.node, self.card, None)
        self.assertNotIn("tg-emoji", self.channel.call.call_args.kwargs["text"])

    async def test_rate_limit_keeps_retry_hint(self):
        self.channel.call = AsyncMock(side_effect=TelegramError("Too Many Requests", 63))
        with self.assertRaises(TelegramError) as raised:
            await self.channel.deliver(self.node, self.card, None)
        self.assertEqual(raised.exception.retry, 63)

    async def test_renaming_topic_is_remembered(self):
        await self.channel.update({"message": {"chat": {"id": -100123}, "message_thread_id": 11,
                                              "forum_topic_edited": {"name": "New name"}}})
        self.assertEqual(self.store.state(self.channel.topic_key(self.node["id"]))["name"], "New name")

    async def test_untrusted_user_and_other_chat_cannot_mute(self):
        self.channel.reply = AsyncMock()
        for chat, user in ((-100123, 99), (-100999, 42)):
            await self.channel.update({"message": {"chat": {"id": chat}, "from": {"id": user}, "text": "/mute 1h", "message_thread_id": 11}})
        self.assertEqual(self.store.db.execute("SELECT muted FROM nodes").fetchone()[0], 0)
        self.channel.reply.assert_not_awaited()

    async def test_owner_mute_is_scoped_to_topic(self):
        self.channel.reply = AsyncMock()
        await self.channel.update({"message": {"chat": {"id": -100123}, "from": {"id": 42}, "text": "/mute 1h", "message_thread_id": 11}})
        self.assertGreater(self.store.db.execute("SELECT muted FROM nodes").fetchone()[0], 0)
