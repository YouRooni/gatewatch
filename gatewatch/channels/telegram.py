import asyncio
import hashlib
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
    def __init__(self, description, retry=30, code=400):
        super().__init__(description)
        self.retry = retry
        self.code = code


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
        if method not in ("getUpdates", "getMe", "getChat", "getChatMember", "answerCallbackQuery"):
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
        except (aiohttp.ClientError, ValueError):
            raise TelegramError("Telegram unavailable", code=0) from None
        if not data.get("ok"):
            raise TelegramError(
                data.get("description", "Telegram error"),
                data.get("parameters", {}).get("retry_after", 30),
                data.get("error_code", response.status),
            )
        return data["result"]

    async def start(self):
        me = await self.call("getMe")
        chat = await self.call("getChat", chat_id=self.chat)
        member = await self.call("getChatMember", chat_id=self.chat, user_id=me["id"])
        if (
            not chat.get("is_forum")
            or member.get("status") != "administrator"
            or not member.get("can_manage_topics")
        ):
            raise ValueError("Telegram requires a forum supergroup and Manage Topics admin permission")
        commands_key = f"telegram:{self.id}:commands"
        if self.store.state(commands_key) != self.lang:
            ru = self.lang == "ru"
            descriptions = (
                ["Серверы", "Этот сервер", "Пауза неудачных входов", "Включить уведомления", "Справка"]
                if ru
                else ["Servers", "This server", "Mute failed sign-ins", "Resume notifications", "Help"]
            )
            await self.call(
                "setMyCommands",
                scope={"type": "chat", "chat_id": self.chat},
                commands=[
                    {"command": command, "description": description}
                    for command, description in zip(
                        ("status", "node", "mute", "unmute", "help"), descriptions, strict=False
                    )
                ],
            )
            self.store.set_state(commands_key, self.lang)
        self.task = asyncio.create_task(self.poll())

    def topic_key(self, node_id):
        return f"telegram:{self.id}:topic:{node_id}"

    async def topic(self, node):
        key = self.topic_key(node["id"])
        topic = self.store.state(key, {})
        if not topic.get("id"):
            default_name = node["name"] + (" - " + node["address"] if node.get("address") else "")
            result = await self.call(
                "createForumTopic", chat_id=self.chat, name=topic.get("name", default_name)[:128]
            )
            topic = {"id": result["message_thread_id"], "name": result["name"]}
            self.store.set_state(key, topic)
        return topic["id"]

    async def deliver(self, node, card, receipt):
        topic = await self.topic(node)
        if receipt and receipt.get("topic") != topic:
            receipt = None
        for _attempt in range(3):
            renderer = render_rich if self.rich else render
            body = renderer(
                node,
                card,
                self.lang,
                self.config.get("timezone", "UTC"),
                self.custom,
                self.geo.lookup(card.get("ip")),
                self.config.get("geo", {}).get("attribution", ""),
                self.config.get("show_node_name", False),
                self.config.get("time_display", "auto"),
                self.geo.country_code(card.get("ip")),
            )
            method = "editMessageText" if receipt else "sendRichMessage" if self.rich else "sendMessage"
            params = dict(chat_id=self.chat, link_preview_options={"is_disabled": True})
            if card.get("ip") and card["kind"] != "event":
                token = hashlib.sha256(f"{self.id}:{node['id']}:{card['id']}".encode()).hexdigest()[:24]
                menu = {
                    "node": node["id"],
                    "ip": card["ip"],
                    "topic": topic,
                    "kind": card["kind"],
                    "service": card.get("service", ""),
                    "created": time.time(),
                }
                self.store.set_state(f"telegram:{self.id}:menu:{token}", menu)
                params["reply_markup"] = self.keyboard(token, menu, node)
            if self.rich:
                params["rich_message"] = {"html": body, "skip_entity_detection": True}
            else:
                params.update(text=body, parse_mode="HTML")
            if receipt:
                params["message_id"] = receipt["message"]
            else:
                params.update(
                    message_thread_id=topic,
                    disable_notification=card["kind"] == "auth.failure"
                    or (card["kind"] == "event" and card.get("level") == "info"),
                )
            try:
                result = await self.call(method, **params)
                return {"topic": topic, "message": result["message_id"]}
            except TelegramError as error:
                description = str(error).lower()
                if "message is not modified" in description and receipt:
                    return receipt
                if "message thread not found" in description or "topic_id_invalid" in description:
                    previous = self.store.state(self.topic_key(node["id"]), {})
                    self.store.set_state(
                        self.topic_key(node["id"]),
                        {
                            "name": previous.get(
                                "name",
                                node["name"] + (" - " + node["address"] if node.get("address") else ""),
                            )
                        },
                    )
                    topic = await self.topic(node)
                    receipt = None
                elif receipt and (
                    "message to edit not found" in description or "message can't be edited" in description
                ):
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

    def keyboard(self, token, menu, node, pending=None, status=None):
        ru = self.lang == "ru"
        buttons = []
        blocked = self.store.state(f"blocked:{node['id']}:{menu['ip']}", {}).get(
            "blocked", menu["kind"] == "access.ban"
        )
        action = "unban" if blocked else "ban"
        targetable = menu.get("service") in ("ssh", "sshd", "demo") or (
            menu.get("service") is None and menu["kind"].startswith("auth.")
        )
        if targetable and action in self.store.control.capabilities(node["id"]):
            if pending:
                buttons.append(
                    {"text": "Выполняется…" if ru else "Pending…", "callback_data": "gw:info:" + pending}
                )
            else:
                buttons.append(
                    {
                        "text": ("Разблокировать" if blocked else "Блокировать")
                        if ru
                        else ("Unban" if blocked else "Ban"),
                        "callback_data": f"gw:{action}:{token}",
                    }
                )
        muted = node["muted"] > time.time()
        buttons.append(
            {
                "text": ("Возобновить" if muted else "Пауза на час")
                if ru
                else ("Resume" if muted else "Mute for 1h"),
                "callback_data": f"gw:{'resume' if muted else 'mute'}:{token}",
            }
        )
        rows = [buttons]
        if status:
            rows.append(
                [
                    {
                        "text": ("Готово" if status["ok"] else "Ошибка · подробнее")
                        if ru
                        else ("Done" if status["ok"] else "Failed · details"),
                        "callback_data": "gw:info:" + status["id"],
                    }
                ]
            )
        return {"inline_keyboard": rows}

    async def callback(self, query):
        ru = self.lang == "ru"
        message = query.get("message", {})

        async def answer(text, alert=False):
            await self.call("answerCallbackQuery", callback_query_id=query["id"], text=text, show_alert=alert)

        if (
            query.get("from", {}).get("id") not in self.owners
            or message.get("chat", {}).get("id") != self.chat
        ):
            await answer("Нет доступа." if ru else "Not authorized.")
            return
        data = query.get("data", "")
        info = re.fullmatch("gw:info:([a-f0-9]{32})", data)
        if info:
            job = self.store.db.execute(
                "SELECT * FROM actions WHERE id=? AND channel=?", (info[1], self.id)
            ).fetchone()
            if not job:
                await answer("Кнопка устарела." if ru else "Button expired.")
            elif not job["result"]:
                await answer("Команда выполняется." if ru else "Command pending.")
            else:
                result = json.loads(job["result"])
                translated = {
                    "Protected address": "Адрес защищён от блокировки.",
                    "Fail2ban unavailable": "Fail2ban недоступен.",
                    "Fail2ban client access denied": "Нет доступа к клиенту Fail2ban.",
                    "Fail2ban client unavailable": "Клиент Fail2ban недоступен.",
                    "Fail2ban timeout": "Fail2ban не ответил вовремя.",
                    "Fail2ban rejected action": "Fail2ban отклонил команду.",
                    "Fail2ban helper unavailable": "Модуль управления недоступен.",
                    "Command expired": "Истёк срок команды.",
                    "IP blocked": "IP заблокирован.",
                    "IP unblocked": "IP разблокирован.",
                    "Demo: firewall unchanged": "Тест выполнен. Firewall не изменялся.",
                }
                await answer(
                    translated.get(result["detail"], result["detail"]) if ru else result["detail"],
                    not result["ok"],
                )
            return
        match = re.fullmatch("gw:(ban|unban|mute|resume):([a-f0-9]{24})", data)
        menu = self.store.state(f"telegram:{self.id}:menu:{match[2]}") if match else None
        if not menu or menu["topic"] != message.get("message_thread_id"):
            await answer("Кнопка устарела." if ru else "Button expired.")
            return
        node = self.store.db.execute(
            "SELECT * FROM nodes WHERE id=? AND enabled=1", (menu["node"],)
        ).fetchone()
        if not node:
            await answer("Нода отключена." if ru else "Node disabled.")
            return
        node = dict(node)
        action, token = match.groups()
        if action in ("mute", "resume"):
            node["muted"] = time.time() + 3600 if action == "mute" else 0
            with self.store.db:
                self.store.db.execute("UPDATE nodes SET muted=? WHERE id=?", (node["muted"], node["id"]))
            await answer(
                ("Пауза на час." if action == "mute" else "Уведомления включены.")
                if ru
                else ("Muted for 1h." if action == "mute" else "Notifications resumed.")
            )
            markup = self.keyboard(token, menu, node)
        else:
            targetable = menu.get("service") in ("ssh", "sshd", "demo") or (
                menu.get("service") is None and menu["kind"].startswith("auth.")
            )
            if not targetable:
                await answer(
                    "Для этого jail управление не включено."
                    if ru
                    else "Actions are not enabled for this jail.",
                    True,
                )
                return
            try:
                job_id = self.store.control.request(
                    node["id"],
                    action,
                    menu["ip"],
                    query["from"]["id"],
                    query["id"],
                    self.id,
                    {"message": message["message_id"], "topic": menu["topic"], "menu": token},
                )
            except ValueError as error:
                labels = {
                    "Node offline": "Нода не на связи.",
                    "Action unavailable": "Управление на ноде не включено.",
                    "Action queue busy": "Очередь занята. Попробуй позже.",
                }
                await answer(labels.get(str(error), "Команда недоступна.") if ru else str(error), True)
                return
            await answer("Команда отправлена." if ru else "Command queued.")
            markup = self.keyboard(token, menu, node, pending=job_id)
        try:
            await self.call(
                "editMessageReplyMarkup",
                chat_id=self.chat,
                message_id=message["message_id"],
                reply_markup=markup,
            )
        except TelegramError as error:
            if "message is not modified" not in str(error).lower():
                raise

    async def action_result(self, job):
        context, result = json.loads(job["context"]), json.loads(job["result"])
        menu = self.store.state(f"telegram:{self.id}:menu:{context['menu']}")
        if not menu:
            return
        node = self.store.db.execute("SELECT * FROM nodes WHERE id=?", (job["node"],)).fetchone()
        if not node:
            return
        node = dict(node)
        if result["ok"]:
            self.store.set_state(
                f"blocked:{job['node']}:{job['ip']}", {"blocked": job["action"] == "ban", "time": time.time()}
            )
        try:
            await self.call(
                "editMessageReplyMarkup",
                chat_id=self.chat,
                message_id=context["message"],
                reply_markup=self.keyboard(context["menu"], menu, node, status=result),
            )
        except TelegramError as error:
            if not any(
                part in str(error).lower()
                for part in (
                    "message is not modified",
                    "message to edit not found",
                    "message_id_invalid",
                    "topic_id_invalid",
                )
            ):
                raise

    async def reply(self, message, text):
        params = dict(
            chat_id=self.chat,
            text=text,
            parse_mode="HTML",
            disable_notification=True,
            link_preview_options={"is_disabled": True},
        )
        if message.get("message_thread_id"):
            params["message_thread_id"] = message["message_thread_id"]
        return await self.call("sendMessage", **params)

    async def update(self, update):
        if "callback_query" in update:
            await self.callback(update["callback_query"])
            return
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
            await self.reply(
                message,
                "<b>Gatewatch</b>\n/status — "
                + (
                    "серверы\n/node — этот сервер\n/mute 1h — тише на час\n/unmute — включить\n\nПауза касается только неудачных входов."
                    if ru
                    else "servers\n/node — this server\n/mute 1h — quiet for an hour\n/unmute — resume\n\nMute affects failed sign-ins only."
                ),
            )
        elif command == "/status":
            lines = ["<b>" + ("Серверы" if ru else "Servers") + "</b>"]
            for node in nodes[:35]:
                mark = "○" if not node["seen"] else "●" if time.time() - node["seen"] < 180 else "!"
                if node["health"] == "degraded":
                    mark = "!"
                age = (
                    f"{int(time.time() - node['seen'])}s"
                    if node["seen"]
                    else ("ожидание" if ru else "waiting")
                )
                lines.append(f"{mark} {escape(node['name'])} · {age}")
            if len(nodes) > 35:
                lines.append(f"+{len(nodes) - 35}")
            await self.reply(message, "\n".join(lines))
        elif command in ("/node", "/mute", "/unmute"):
            node = next(
                (
                    node
                    for node in nodes
                    if self.store.state(self.topic_key(node["id"]), {}).get("id") == topic_id
                ),
                None,
            )
            if not node:
                await self.reply(message, "Открой тему сервера." if ru else "Open a server topic.")
                return
            if command == "/node":
                agent_info = self.store.state("agent:" + node["id"], {})
                pending = self.store.db.execute(
                    "SELECT COUNT(*) FROM cards c JOIN deliveries d ON c.id=d.card WHERE c.node=? AND d.channel=? AND d.version<c.version",
                    (node["id"], self.id),
                ).fetchone()[0]
                await self.reply(
                    message,
                    f"<b>{escape(node['name'])}</b>\n<code>{node['id']}</code>\n"
                    + (
                        (f"В очереди: {pending}\nПауза: " + ("да" if node["muted"] > time.time() else "нет"))
                        if ru
                        else f"Queued: {pending}\nMuted: {node['muted'] > time.time()}"
                    )
                    + "\n"
                    + ("Версия: " if ru else "Version: ")
                    + escape(agent_info.get("version", "legacy")),
                )
                return
            duration = 0
            if command == "/mute":
                match = re.fullmatch(r"([1-9][0-9]{0,2})(m|h|d)", parts[1] if len(parts) > 1 else "1h")
                if not match or int(match[1]) * {"m": 60, "h": 3600, "d": 86400}[match[2]] > 7 * 86400:
                    await self.reply(
                        message, "Пример: /mute 1h · максимум 7d" if ru else "Example: /mute 1h · maximum 7d"
                    )
                    return
                duration = int(match[1]) * {"m": 60, "h": 3600, "d": 86400}[match[2]]
            with self.store.db:
                self.store.db.execute(
                    "UPDATE nodes SET muted=? WHERE id=?",
                    (time.time() + duration if duration else 0, node["id"]),
                )
            await self.reply(
                message,
                ("Неудачные входы: пауза." if duration else "Уведомления включены.")
                if ru
                else ("Failed sign-ins muted." if duration else "Notifications resumed."),
            )

    async def poll(self):
        key = f"telegram:{self.id}:offset"
        offset = self.store.state(key, 0)
        while True:
            try:
                updates = await self.call(
                    "getUpdates", offset=offset, timeout=30, allowed_updates=["message", "callback_query"]
                )
                for update in updates:
                    try:
                        await self.update(update)
                    except TelegramError as error:
                        if error.code in (0, 429) or error.code >= 500:
                            raise
                        log.warning("Skipped expired Telegram update: %s", update.get("update_id"))
                    except (KeyError, TypeError, ValueError):
                        log.warning("Skipped malformed Telegram update: %s", update.get("update_id"))
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
