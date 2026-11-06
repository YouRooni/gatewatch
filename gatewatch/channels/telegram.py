import asyncio
import json
import logging
import re
import time
from html import escape

import aiohttp

from ..enrichment import Geo
from ..render import render, render_rich

log = logging.getLogger(__name__)


class TelegramError(Exception):
    def __init__(self, description, retry=30):
        super().__init__(description)
        self.retry = retry


class TelegramChannel:
    def __init__(self, config, store, channel_id):
        self.config, self.store, self.id = config, store, channel_id
        self.session = None
        self.lock = asyncio.Lock()
        self.last_request = 0
        self.custom = config.get("custom_emoji", True)
        self.rich = config.get("rich_messages", True)
        self.geo = Geo(config.get("geo", {}))
        self.chat = int(config["chat_id"])
        self.owners = set(config["owner_ids"])
        self.lang = config.get("language", "ru")
        self.task = None

    async def call(self, method, **params):
        if self.session is None:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45))
        if method not in ("getUpdates", "getMe", "getChat", "getChatMember"):
            async with self.lock:
                await asyncio.sleep(max(0, self.last_request + 3.2 - time.monotonic()))
                self.last_request = time.monotonic()
                return await self._request(method, params)
        return await self._request(method, params)

    async def _request(self, method, params):
        url = "https://api.telegram.org/bot" + self.config["token"] + "/" + method
        try:
            async with self.session.post(url, json=params) as response:
                data = await response.json()
        except (aiohttp.ClientError, ValueError) as error:
            raise TelegramError("Telegram unavailable") from None
        if not data.get("ok"):
            raise TelegramError(data.get("description", "Telegram error"), data.get("parameters", {}).get("retry_after", 30))
        return data["result"]

    async def start(self):
        me = await self.call("getMe")
        chat = await self.call("getChat", chat_id=self.chat)
        member = await self.call("getChatMember", chat_id=self.chat, user_id=me["id"])
        if not chat.get("is_forum") or member.get("status") != "administrator" or not member.get("can_manage_topics"):
            raise ValueError("Telegram requires a forum supergroup and Manage Topics admin permission")
        commands_key = f"telegram:{self.id}:commands"
        if self.store.state(commands_key) != self.lang:
            ru = self.lang == "ru"
            descriptions = ["Серверы", "Этот сервер", "Пауза неудачных входов", "Включить уведомления", "Справка"] if ru else ["Servers", "This server", "Mute failed sign-ins", "Resume notifications", "Help"]
            await self.call("setMyCommands", scope={"type": "chat", "chat_id": self.chat},
                            commands=[{"command": command, "description": description} for command, description in zip(("status", "node", "mute", "unmute", "help"), descriptions)])
            self.store.set_state(commands_key, self.lang)
        self.task = asyncio.create_task(self.poll())

    def topic_key(self, node_id):
        return f"telegram:{self.id}:topic:{node_id}"

    async def topic(self, node):
        key = self.topic_key(node["id"])
        topic = self.store.state(key, {})
        if not topic.get("id"):
            default_name = node["name"] + (" - " + node["address"] if node.get("address") else "")
            result = await self.call("createForumTopic", chat_id=self.chat, name=topic.get("name", default_name)[:128])
            topic = {"id": result["message_thread_id"], "name": result["name"]}
            self.store.set_state(key, topic)
        return topic["id"]

    async def deliver(self, node, card, receipt):
        topic = await self.topic(node)
        if receipt and receipt.get("topic") != topic:
            receipt = None
        for attempt in range(3):
            renderer = render_rich if self.rich else render
            body = renderer(node, card, self.lang, self.config.get("timezone", "UTC"), self.custom,
                          self.geo.lookup(card.get("ip")), self.config.get("geo", {}).get("attribution", ""),
                          self.config.get("show_node_name", False))
            method = "editMessageText" if receipt else "sendRichMessage" if self.rich else "sendMessage"
            params = dict(chat_id=self.chat)
            if self.rich:
                params["rich_message"] = {"html": body, "skip_entity_detection": True}
            else:
                params.update(text=body, parse_mode="HTML")
            if receipt:
                params["message_id"] = receipt["message"]
            else:
                params.update(message_thread_id=topic, disable_notification=card["kind"] == "auth.failure")
            try:
                result = await self.call(method, **params)
                return {"topic": topic, "message": result["message_id"]}
            except TelegramError as error:
                description = str(error).lower()
                if "message is not modified" in description and receipt:
                    return receipt
                if "message thread not found" in description or "topic_id_invalid" in description:
                    previous = self.store.state(self.topic_key(node["id"]), {})
                    self.store.set_state(self.topic_key(node["id"]), {"name": previous.get("name", node["name"] + (" - " + node["address"] if node.get("address") else ""))})
                    topic = await self.topic(node)
                    receipt = None
                elif receipt and ("message to edit not found" in description or "message can't be edited" in description):
                    receipt = None
                elif self.custom and ("custom emoji" in description or "emoji_id_invalid" in description):
                    self.custom = False
                    log.warning("Custom emoji unavailable; using standard icons")
                elif self.rich and ("method not found" in description or description == "not found"):
                    self.rich = False
                    log.warning("Rich messages unavailable; using standard HTML")
                elif "topic_closed" in description:
                    error.retry = 300
                    raise
                else:
                    raise
        raise TelegramError("Delivery retry exhausted")

    async def reply(self, message, text):
        params = dict(chat_id=self.chat, text=text, parse_mode="HTML", disable_notification=True)
        if message.get("message_thread_id"):
            params["message_thread_id"] = message["message_thread_id"]
        return await self.call("sendMessage", **params)

    async def update(self, update):
        message = update.get("message", {})
        if message.get("chat", {}).get("id") != self.chat:
            return
        topic_id = message.get("message_thread_id")
        edited = message.get("forum_topic_edited", {})
        if edited.get("name"):
            for row in self.store.db.execute("SELECT id FROM nodes"):
                key = self.topic_key(row[0])
                topic = self.store.state(key, {})
                if topic.get("id") == topic_id:
                    topic["name"] = edited["name"]
                    self.store.set_state(key, topic)
                    break
        if message.get("from", {}).get("id") not in self.owners:
            return
        parts = message.get("text", "").split()
        if not parts or not parts[0].startswith("/"):
            return
        command = parts[0].split("@")[0]
        ru = self.lang == "ru"
        nodes = self.store.db.execute("SELECT * FROM nodes WHERE enabled=1 ORDER BY name").fetchall()
        if command in ("/start", "/help"):
            await self.reply(message, "<b>Gatewatch</b>\n/status — " + ("серверы\n/node — этот сервер\n/mute 1h — тише на час\n/unmute — включить\n\nПауза касается только неудачных входов." if ru else "servers\n/node — this server\n/mute 1h — quiet for an hour\n/unmute — resume\n\nMute affects failed sign-ins only."))
        elif command == "/status":
            lines = ["<b>" + ("Серверы" if ru else "Servers") + "</b>"]
            for node in nodes[:35]:
                mark = "○" if not node["seen"] else "●" if time.time() - node["seen"] < 180 else "!"
                age = f"{int(time.time() - node['seen'])}s" if node["seen"] else ("ожидание" if ru else "waiting")
                lines.append(f"{mark} {escape(node['name'])} · {age}")
            if len(nodes) > 35:
                lines.append(f"+{len(nodes)-35}")
            await self.reply(message, "\n".join(lines))
        elif command in ("/node", "/mute", "/unmute"):
            node = next((node for node in nodes if self.store.state(self.topic_key(node["id"]), {}).get("id") == topic_id), None)
            if not node:
                await self.reply(message, "Открой тему сервера." if ru else "Open a server topic.")
                return
            if command == "/node":
                pending = self.store.db.execute("SELECT COUNT(*) FROM cards c JOIN deliveries d ON c.id=d.card WHERE c.node=? AND d.channel=? AND d.version<c.version", (node["id"], self.id)).fetchone()[0]
                await self.reply(message, f"<b>{escape(node['name'])}</b>\n<code>{node['id']}</code>\n" + ((f"В очереди: {pending}\nПауза: " + ("да" if node["muted"] > time.time() else "нет")) if ru else f"Queued: {pending}\nMuted: {node['muted'] > time.time()}"))
                return
            duration = 0
            if command == "/mute":
                match = re.fullmatch(r"([1-9][0-9]{0,2})(m|h|d)", parts[1] if len(parts) > 1 else "1h")
                if not match or int(match[1]) * {"m": 60, "h": 3600, "d": 86400}[match[2]] > 7 * 86400:
                    await self.reply(message, "Пример: /mute 1h · максимум 7d" if ru else "Example: /mute 1h · maximum 7d")
                    return
                duration = int(match[1]) * {"m": 60, "h": 3600, "d": 86400}[match[2]]
            with self.store.db:
                self.store.db.execute("UPDATE nodes SET muted=? WHERE id=?", (time.time() + duration if duration else 0, node["id"]))
            await self.reply(message, ("Неудачные входы: пауза." if duration else "Уведомления включены.") if ru else ("Failed sign-ins muted." if duration else "Notifications resumed."))

    async def poll(self):
        key = f"telegram:{self.id}:offset"
        offset = self.store.state(key, 0)
        while True:
            try:
                updates = await self.call("getUpdates", offset=offset, timeout=30, allowed_updates=["message"])
                for update in updates:
                    await self.update(update)
                    offset = update["update_id"] + 1
                    self.store.set_state(key, offset)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log.warning("Telegram updates postponed: %s", type(error).__name__)
                await asyncio.sleep(max(5, getattr(error, "retry", 5)))

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        if self.session:
            await self.session.close()
        self.geo.close()
