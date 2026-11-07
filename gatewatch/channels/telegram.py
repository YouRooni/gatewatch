import asyncio
import hashlib
import json
import logging
import re
import time
import uuid
from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

import aiohttp

from ..enrichment import Geo
from ..render import render, render_rich
from ..targets import ActionTarget

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
        self.retry_until = 0
        self.custom = config.get("custom_emoji", True)
        self.rich = config.get("rich_messages", True)
        self.geo = Geo(config.get("geo", {}))
        self.chat = int(config["chat_id"])
        self.owners = set(config["owner_ids"])
        self.lang = config.get("language", "ru")
        self.task = None
        self.username = ""
        self.message_locks = [asyncio.Lock() for _ in range(64)]
        self.refreshes = {}
        self.refresh_task = None
        self.poll_healthy = True

    async def call(self, method, **params):
        if self.session is None:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45))
        if method not in ("getUpdates", "getMe", "getChat", "getChatMember", "answerCallbackQuery"):
            async with self.lock:
                remaining = self.retry_until - time.monotonic()
                if remaining > 0:
                    raise TelegramError("Telegram rate limited", retry=remaining, code=429)
                await asyncio.sleep(max(0, self.last_request + 3.2 - time.monotonic()))
                self.last_request = time.monotonic()
                try:
                    return await self._request(method, params)
                except TelegramError as error:
                    if error.code == 429:
                        self.retry_until = time.monotonic() + error.retry
                    raise
        return await self._request(method, params)

    async def _request(self, method, params):
        url = "https://api.telegram.org/bot" + self.config["token"] + "/" + method
        try:
            async with self.session.post(url, json=params) as response:
                data = await response.json()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            raise TelegramError("Telegram unavailable", code=0) from None
        except ValueError:
            raise TelegramError("Invalid Telegram response", code=response.status) from None
        if not data.get("ok"):
            raise TelegramError(
                data.get("description", "Telegram error"),
                data.get("parameters", {}).get("retry_after", 30),
                data.get("error_code", response.status),
            )
        return data["result"]

    async def start(self):
        me = await self.call("getMe")
        self.username = me.get("username", "").casefold()
        chat = await self.call("getChat", chat_id=self.chat)
        member = await self.call("getChatMember", chat_id=self.chat, user_id=me["id"])
        if (
            not chat.get("is_forum")
            or member.get("status") != "administrator"
            or not member.get("can_manage_topics")
        ):
            raise PermissionError("Telegram requires a forum supergroup and Manage Topics admin permission")
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
        async with self.message_lock(card["id"]):
            card = self.store.card_payload(node["id"], card["id"]) or card
            return await self.present(node, card, receipt)

    def message_lock(self, card_id):
        return self.message_locks[hash(card_id) % len(self.message_locks)]

    def menu_key(self, token):
        return f"telegram:{self.id}:menu:{token}"

    async def present(self, node, card, receipt, *, recreate=True):
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
            menu = None
            if card.get("ip") and card["kind"].startswith(("auth.", "access.")):
                token = hashlib.sha256(f"{self.id}:{node['id']}:{card['id']}".encode()).hexdigest()[:24]
                previous = self.store.state(self.menu_key(token), {})
                menu = {
                    **previous,
                    **{
                        "node": node["id"],
                        "ip": card["ip"],
                        "topic": topic,
                        "card": card["id"],
                        "kind": card["kind"],
                        "service": card.get("service", ""),
                        "created": previous.get("created", time.time()),
                    },
                }
                if not receipt:
                    menu.pop("message", None)
                    menu.pop("view", None)
                panel, rows = self.ip_view(token, menu, node)
                if panel:
                    body = (
                        panel
                        if menu.get("view") or not body
                        else body + ("" if self.rich else "\n\n") + panel
                    )
                params["reply_markup"] = {"inline_keyboard": rows}
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
                if menu is not None:
                    self.save_menu(token, menu, result["message_id"], receipt)
                return {"topic": topic, "message": result["message_id"]}
            except TelegramError as error:
                description = str(error).lower()
                if "message is not modified" in description and receipt:
                    if menu is not None:
                        self.save_menu(token, menu, receipt["message"], receipt)
                    return receipt
                if "message thread not found" in description or "topic_id_invalid" in description:
                    if not recreate:
                        return receipt
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
                    "message to edit not found" in description
                    or "message can't be edited" in description
                    or "message_id_invalid" in description
                ):
                    if not recreate:
                        return receipt
                    receipt = None
                elif self.custom and ("custom emoji" in description or "emoji_id_invalid" in description):
                    self.custom = False
                    log.warning("Custom emoji unavailable; using standard icons")
                elif self.rich and ("method not found" in description or description == "not found"):
                    self.rich = False
                    log.warning("Rich messages unavailable; using standard HTML")
                elif "topic_closed" in description or "topic closed" in description:
                    error.retry = 300
                    raise
                else:
                    raise
        raise TelegramError("Delivery retry exhausted")

    def save_menu(self, token, menu, message_id, receipt):
        key = self.menu_key(token)
        current = self.store.state(key, {})
        if receipt and current.get("message") == receipt["message"]:
            for field in ("view", "confirmation", "action_job"):
                menu.pop(field, None)
                if field in current:
                    menu[field] = current[field]
        self.store.set_state(key, {**menu, "message": message_id})

    def say(self, ru, en):
        return ru if self.lang == "ru" else en

    def stamp(self, timestamp):
        zone = ZoneInfo(self.config.get("timezone", "UTC"))
        return datetime.fromtimestamp(timestamp, zone).strftime("%d.%m · %H:%M:%S %Z")

    @staticmethod
    def button(text, data):
        return {"text": text, "callback_data": data}

    def ip_state(self, menu):
        target = ActionTarget.command(menu["node"], self.store.control.adapter(menu["node"]), menu["ip"])
        job = self.store.control.latest(target)
        access = self.store.state(target.key, {})
        state = self.store.control.action_state(job, time.time()) if job else ""
        if state == "unknown" and self.store.control.reconciled(target, job):
            state = "expired"
        action = "unban" if access.get("blocked") else "ban"
        if state in ("queued", "running"):
            action = job["action"]
        return target, job, access, state, action

    def ip_view(self, token, menu, node):
        adapter = self.store.control.adapter(node["id"])
        if menu.get("service") not in ("ssh", "sshd", "demo") or adapter not in ("demo", "fail2ban"):
            return "", []
        if not self.store.control.capabilities(node["id"]) and not menu.get("view"):
            return "", []
        target, job, access, state, action = self.ip_state(menu)
        view = menu.get("view", "")
        open_button = self.button(self.say("Управление IP", "Manage IP"), f"gw:ip:{token}")
        pending = state in ("queued", "running")
        result = self.job_text(job, state)
        if not view:
            subscribed = job and menu.get("action_job") == job["id"]
            return self.panel_body([escape(result)]) if subscribed and result else "", [[open_button]]
        jail = "Demo" if adapter == "demo" else "SSH / sshd"
        scope = f"<code>{escape(target.ip)}</code> · {escape(node['name'][:128])} · {jail}"
        observation = self.say("Последнее наблюдение: неизвестно", "Last observation: unknown")
        if adapter == "demo":
            observation = self.say(
                "Тестовый режим. Firewall без изменений.", "Demo mode. Firewall is unchanged."
            )
        elif access:
            status = (
                self.say("заблокирован", "blocked")
                if access["blocked"]
                else self.say("разблокирован", "unblocked")
            )
            observation = self.say("Последнее наблюдение: ", "Last observation: ") + status
            observation += " · " + self.stamp(access.get("observed_at", access["time"]))
        lines = [f"<b>{self.say('Управление IP', 'Manage IP')}</b>", scope, escape(observation)]
        back = self.button(self.say("Назад", "Back"), f"gw:back:{token}")
        refresh = self.button(self.say("Обновить", "Refresh"), f"gw:ip:{token}")
        label = self.say("Разблокировать", "Unban") if action == "unban" else self.say("Заблокировать", "Ban")
        rows = [[refresh, back]]
        if result:
            lines.append(escape(result))
        if pending:
            rows = [[self.button(self.say("Выполняется…", "Pending…"), f"gw:ip:{token}"), back]]
        elif state != "unknown" and action in self.store.control.capabilities(node["id"]):
            confirmation = menu.get("confirmation", {})
            if confirmation.get("nonce") and self.valid_confirmation(
                confirmation, job, access, state, action
            ):
                lines.extend(
                    [
                        f"<b>{label}?</b>",
                        self.say("Тест без изменений в firewall.", "Demo action. Firewall is unchanged.")
                        if adapter == "demo"
                        else self.say("Изменится только jail sshd.", "Only the sshd jail will change."),
                    ]
                )
                rows = [
                    [
                        self.button(label, f"gw:ok:{token}:{confirmation['nonce']}"),
                        self.button(self.say("Отмена", "Cancel"), f"gw:ip:{token}"),
                    ]
                ]
            else:
                rows = [[self.button(label, f"gw:ask:{token}"), back]]
        return self.panel_body(lines), rows

    def panel_body(self, lines):
        return "".join(f"<p>{line}</p>" for line in lines) if self.rich else "\n".join(lines)

    def job_text(self, job, state):
        if state in ("queued", "running"):
            return self.say(
                "Команда отправлена. Ожидаем ответ сервера.", "Command queued. Waiting for the server."
            )
        if state == "unknown":
            return self.say(
                "Сервер не подтвердил результат. Обнови наблюдение позже.",
                "The server did not confirm the result. Refresh the observation later.",
            )
        if state == "expired":
            return self.say("Истёк срок команды.", "Command expired.")
        if not job or not job["result"]:
            return ""
        detail = json.loads(job["result"])["detail"]
        labels = {
            "Protected address": ("Адрес защищён от блокировки.", "Protected address."),
            "Fail2ban unavailable": ("Fail2ban недоступен.", "Fail2ban unavailable."),
            "Fail2ban client access denied": (
                "Нет доступа к клиенту Fail2ban.",
                "Fail2ban client access denied.",
            ),
            "Fail2ban client unavailable": ("Клиент Fail2ban недоступен.", "Fail2ban client unavailable."),
            "Fail2ban timeout": ("Fail2ban не ответил вовремя.", "Fail2ban timed out."),
            "Fail2ban rejected action": ("Fail2ban отклонил команду.", "Fail2ban rejected the action."),
            "Fail2ban helper unavailable": ("Модуль управления недоступен.", "Action helper unavailable."),
            "Demo: firewall unchanged": (
                "Тест выполнен. Firewall без изменений.",
                "Demo completed. Firewall unchanged.",
            ),
        }
        if detail in labels:
            return self.say(*labels[detail])
        if state == "succeeded":
            return (
                self.say("SSH-блокировка установлена.", "SSH ban applied.")
                if job["action"] == "ban"
                else self.say("SSH-блокировка снята.", "SSH ban removed.")
            )
        return self.say("Команда не выполнена.", "Command failed.")

    @staticmethod
    def valid_confirmation(confirmation, job, access, state, action):
        if time.time() >= confirmation.get("expires", 0) or action != confirmation.get("action"):
            return False
        if access.get("time") != confirmation.get("observed"):
            return False
        if job and state in ("queued", "running"):
            return job["action"] == action and job["created"] >= confirmation["created"]
        return confirmation.get("job") == (job["id"] if job else None) and state == confirmation.get("state")

    async def redraw(self, menu, node):
        card = self.store.card_payload(node["id"], menu["card"])
        if card:
            await self.present(
                node,
                card,
                {"message": menu["message"], "topic": menu["topic"]},
                recreate=False,
            )

    def queue_redraw(self, token, message):
        if len(self.refreshes) >= 128 and token not in self.refreshes:
            self.refreshes.pop(next(iter(self.refreshes)))
        self.refreshes[token] = message
        if self.refresh_task is None or self.refresh_task.done():
            self.refresh_task = asyncio.create_task(self.refresh_cards())

    async def refresh_cards(self):
        while self.refreshes:
            token = next(iter(self.refreshes))
            message = self.refreshes.pop(token)
            key = self.menu_key(token)
            menu = self.store.state(key, {})
            if not self.valid_menu(menu, message):
                continue
            try:
                async with self.message_lock(menu["card"]):
                    menu = self.store.state(key, {})
                    if not self.valid_menu(menu, message):
                        continue
                    node = self.store.db.execute(
                        "SELECT * FROM nodes WHERE id=? AND enabled=1", (menu["node"],)
                    ).fetchone()
                    if node:
                        await self.redraw(menu, dict(node))
            except TelegramError as error:
                if error.code == 429:
                    self.queue_redraw(token, message)
                    await asyncio.sleep(error.retry)
                else:
                    log.warning("Telegram card refresh postponed: code %s", error.code)

    async def callback(self, query):
        message = query.get("message", {})

        async def answer(text="", alert=False):
            await self.call("answerCallbackQuery", callback_query_id=query["id"], text=text, show_alert=alert)

        if (
            query.get("from", {}).get("id") not in self.owners
            or message.get("chat", {}).get("id") != self.chat
        ):
            await answer(self.say("Нет доступа.", "Not authorized."))
            return
        expired = self.say("Кнопка устарела.", "Button expired.")
        if message.get("date") == 0 or not message.get("message_id"):
            await answer(expired)
            return
        data = query.get("data", "")
        if await self.dashboard_callback(data, message, answer):
            return
        match = re.fullmatch(r"gw:(ip|back|ask|ok):([a-f0-9]{24})(?::([a-f0-9]{12}))?", data)
        if not match:
            await answer(expired)
            return
        operation, token, nonce = match.groups()
        key = self.menu_key(token)
        menu = self.store.state(key, {})
        if not self.valid_menu(menu, message):
            await answer(expired)
            return
        node = self.store.db.execute(
            "SELECT * FROM nodes WHERE id=? AND enabled=1", (menu["node"],)
        ).fetchone()
        if not node:
            await answer(self.say("Сервер отключён.", "Server disabled."))
            return
        node = dict(node)
        if self.store.control.adapter(node["id"]) not in ("fail2ban", "demo"):
            await answer(self.say("Управление IP недоступно.", "IP actions unavailable."))
            return
        target, job, access, state, action = self.ip_state(menu)
        if operation == "ok":
            confirmation = menu.get("confirmation", {})
            if nonce != confirmation.get("nonce") or not self.valid_confirmation(
                confirmation, job, access, state, action
            ):
                await answer(expired)
                return
            try:
                job_id = self.store.control.request(
                    node["id"],
                    action,
                    target.ip,
                    query["from"]["id"],
                    f"{self.id}:{query['id']}",
                    self.id,
                    {"message": message["message_id"], "topic": menu["topic"], "menu": token},
                )
            except ValueError as error:
                labels = {
                    "Node offline": "Сервер не на связи.",
                    "Action unavailable": "Управление на сервере недоступно.",
                    "Action queue busy": "Очередь команд занята.",
                    "Another action is pending": "Предыдущая команда выполняется.",
                    "Previous action is unconfirmed": "Сервер не подтвердил предыдущую команду.",
                }
                await answer(
                    labels.get(str(error), "Команда недоступна.") if self.lang == "ru" else str(error), True
                )
                return
            menu.pop("confirmation", None)
            menu["action_job"] = job_id
            self.store.set_state(key, menu)
            await answer(self.say("Команда принята.", "Command queued."))
        else:
            menu.pop("confirmation", None)
            menu["view"] = "" if operation == "back" else "manage"
            if operation == "ask" and state not in ("queued", "running", "unknown"):
                now = time.time()
                menu["confirmation"] = {
                    "nonce": uuid.uuid4().hex[:12],
                    "action": action,
                    "observed": access.get("time"),
                    "job": job["id"] if job else None,
                    "state": state,
                    "created": now,
                    "expires": now + 120,
                }
            self.store.set_state(key, menu)
            await answer()
        self.queue_redraw(token, message)

    def valid_menu(self, menu, message):
        return (
            menu.get("card") is not None
            and menu.get("service") in ("ssh", "sshd", "demo")
            and menu.get("message") == message["message_id"]
            and menu.get("topic") == message.get("message_thread_id")
            and self.store.state(self.topic_key(menu["node"]), {}).get("id") == menu["topic"]
            and time.time() - menu["created"] < 14 * 86400
        )

    async def action_result(self, job):
        context = json.loads(job.get("subscriber_context", job["context"]))
        token = context.get("menu")
        menu = self.store.state(self.menu_key(token), {})
        if not menu.get("card") or any(menu.get(key) != context.get(key) for key in ("message", "topic")):
            return
        node = self.store.db.execute(
            "SELECT * FROM nodes WHERE id=? AND enabled=1", (job["node"],)
        ).fetchone()
        if node:
            async with self.message_lock(menu["card"]):
                latest = self.store.state(self.menu_key(token), {})
                if latest.get("message") == context["message"]:
                    await self.redraw(latest, dict(node))

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
        command, mention, username = parts[0].partition("@")
        if mention and (not self.username or username.casefold() != self.username):
            return
        if command in ("/start", "/help"):
            await self.reply(
                message,
                self.say(
                    "<b>Gatewatch</b>\n/status — серверы\n/node — панель сервера\n/mute 1h — пауза неудачных входов во всех каналах\n/unmute — возобновить эти уведомления",
                    "<b>Gatewatch</b>\n/status — servers\n/node — server panel\n/mute 1h — pause failed sign-ins across all channels\n/unmute — resume these notifications",
                ),
            )
        elif command == "/status":
            await self.show_status(message)
        elif command in ("/node", "/mute", "/unmute"):
            node = next(
                (
                    row
                    for row in self.nodes()
                    if topic_id and self.store.state(self.topic_key(row["id"]), {}).get("id") == topic_id
                ),
                None,
            )
            if not node:
                await self.reply(message, self.say("Открой тему сервера.", "Open a server topic."))
                return
            if command == "/node":
                await self.show_node(node, topic_id)
                return
            duration = 0
            if command == "/mute":
                match = re.fullmatch(r"([1-9][0-9]{0,2})(m|h|d)", parts[1] if len(parts) > 1 else "1h")
                duration = int(match[1]) * {"m": 60, "h": 3600, "d": 86400}[match[2]] if match else 0
                if not 0 < duration <= 7 * 86400:
                    await self.reply(
                        message, self.say("Пример: /mute 1h · максимум 7d", "Example: /mute 1h · maximum 7d")
                    )
                    return
            self.store.mute(node["id"], duration)
            await self.show_node(node, topic_id)

    def nodes(self):
        return self.store.db.execute("SELECT * FROM nodes WHERE enabled=1 ORDER BY name,id").fetchall()

    def status_label(self, node):
        now = time.time()
        labels = {
            "new": ("Ожидает связи", "Waiting for agent"),
            "online": ("Агент на связи", "Agent connected"),
            "degraded": ("Сбор нарушен", "Collection degraded"),
            "offline": ("Нет связи с агентом", "Agent unreachable"),
        }
        label = self.say(*labels[self.store.node_status(node, now)])
        if node["seen"]:
            age = max(0, int(now - node["seen"]))
            label += f" · {age} " + self.say("с назад", "s ago")
        return label

    async def show_status(self, message, page=0, edit=False):
        nodes = self.nodes()
        size = 15
        pages = max(1, (len(nodes) + size - 1) // size)
        page = min(page, pages - 1)
        lines = [self.say("<b>Серверы</b>", "<b>Servers</b>")]
        rows = []
        if not nodes:
            lines.append(
                self.say(
                    "Добавь сервер через gatewatch node add и подключи агент.",
                    "Run gatewatch node add and connect an agent.",
                )
            )
        for node in nodes[page * size : (page + 1) * size]:
            name = escape(node["name"][:85])
            topic = self.store.state(self.topic_key(node["id"]), {}).get("id")
            if topic:
                name = f'<a href="https://t.me/c/{-self.chat - 10**12}/{topic}">{name}</a>'
            lines.append(f"{name} · {self.status_label(node)}")
        if pages > 1:
            lines.append(f"{page + 1}/{pages}")
            if page:
                rows.append(self.button(self.say("Назад", "Previous"), f"gw:status:{page - 1}"))
            if page + 1 < pages:
                rows.append(self.button(self.say("Далее", "Next"), f"gw:status:{page + 1}"))
        await self.dashboard_message(
            message.get("message_thread_id"),
            "\n".join(lines),
            [rows] if rows else [],
            message["message_id"] if edit else None,
        )

    async def dashboard_message(self, topic, body, rows, message_id=None):
        params = {
            "chat_id": self.chat,
            "text": body,
            "parse_mode": "HTML",
            "reply_markup": {"inline_keyboard": rows},
            "link_preview_options": {"is_disabled": True},
        }
        if message_id:
            try:
                return await self.call("editMessageText", message_id=message_id, **params)
            except TelegramError as error:
                description = str(error).lower()
                if "message is not modified" in description:
                    return {"message_id": message_id}
                if not any(
                    part in description
                    for part in ("message to edit not found", "message_id_invalid", "message can't be edited")
                ):
                    raise
        if topic:
            params["message_thread_id"] = topic
        return await self.call("sendMessage", disable_notification=True, **params)

    async def show_node(self, node, topic, view="main"):
        token = hashlib.sha256(f"{self.id}:node:{node['id']}".encode()).hexdigest()[:24]
        key = f"telegram:{self.id}:panel:{token}"
        async with self.message_lock(token):
            node = self.store.db.execute("SELECT * FROM nodes WHERE id=?", (node["id"],)).fetchone()
            if not node or not node["enabled"]:
                return
            panel = self.store.state(key, {})
            info = self.store.state("agent:" + node["id"], {})
            sources = info.get("sources", {})
            pending = self.store.db.execute(
                "SELECT COUNT(*) FROM deliveries d JOIN cards c ON c.id=d.card WHERE c.node=? AND d.channel=? AND d.dirty=1",
                (node["id"], self.id),
            ).fetchone()[0]
            lines = [f"<b>{escape(node['name'][:128])}</b>", self.status_label(node)]
            if sources and view != "details":
                if "error" in sources.values():
                    lines.append(self.say("Сбор событий: есть ошибки", "Event collection has errors"))
                elif "starting" in sources.values():
                    lines.append(self.say("Сбор событий: запуск", "Event collection starting"))
                else:
                    lines.append(self.say("Сбор событий: работает", "Event collection ready"))
            if sources and view == "details":
                source_labels = {
                    "ok": self.say("работает", "ready"),
                    "error": self.say("ошибка", "error"),
                    "starting": self.say("запуск", "starting"),
                }
                lines.extend(
                    f"{escape(key)} · {source_labels[value]}" for key, value in sorted(sources.items())
                )
            if view == "details":
                lines.append(
                    self.say("Версия агента: ", "Agent version: ") + escape(info.get("version", "legacy"))
                )
            lines.append(self.say(f"Ожидают доставки: {pending}", f"Queued for delivery: {pending}"))
            muted = node["muted"] > time.time()
            lines.append(
                self.say(
                    "Неудачные входы: пауза во всех каналах до ",
                    "Failed sign-ins: paused across all channels until ",
                )
                + escape(self.stamp(node["muted"]))
                if muted
                else self.say("Неудачные входы: уведомления включены", "Failed sign-in notifications enabled")
            )

            def make(label, operation):
                return self.button(label, f"gw:node:{token}:{operation}")

            if view == "mute":
                lines.append(
                    self.say(
                        "На сколько приостановить уведомления о неудачных входах во всех каналах? Сбор и блокировки продолжатся.",
                        "Pause failed sign-in notifications across all channels. Collection and bans continue.",
                    )
                )
                rows = [
                    [
                        make(self.say("15 минут", "15 min"), "15m"),
                        make(self.say("1 час", "1 hour"), "1h"),
                        make(self.say("8 часов", "8 hours"), "8h"),
                    ],
                    [make(self.say("Назад", "Back"), "main")],
                ]
            else:
                rows = [
                    [
                        make(self.say("Возобновить", "Resume"), "resume")
                        if muted
                        else make(self.say("Приостановить", "Pause notifications"), "mute")
                    ],
                    [
                        make(self.say("Обновить", "Refresh"), "main"),
                        make(self.say("Подробнее", "Details"), "details"),
                    ],
                ]
            result = await self.dashboard_message(
                topic, "\n".join(lines), rows, panel.get("message") if panel.get("topic") == topic else None
            )
            self.store.set_state(key, {"node": node["id"], "topic": topic, "message": result["message_id"]})

    async def dashboard_callback(self, data, message, answer):
        status = re.fullmatch(r"gw:status:([0-9]{1,6})", data)
        if status:
            await answer()
            await self.show_status(message, int(status[1]), edit=True)
            return True
        match = re.fullmatch(r"gw:node:([a-f0-9]{24}):(main|details|mute|resume|15m|1h|8h)", data)
        if not match:
            return False
        panel = self.store.state(f"telegram:{self.id}:panel:{match[1]}", {})
        if (
            panel.get("message") != message["message_id"]
            or panel.get("topic") != message.get("message_thread_id")
            or self.store.state(self.topic_key(panel["node"]), {}).get("id") != panel["topic"]
        ):
            await answer(self.say("Кнопка устарела.", "Button expired."))
            return True
        node = self.store.db.execute(
            "SELECT * FROM nodes WHERE id=? AND enabled=1", (panel["node"],)
        ).fetchone()
        if not node:
            await answer(self.say("Сервер отключён.", "Server disabled."))
            return True
        operation = match[2]
        durations = {"resume": 0, "15m": 900, "1h": 3600, "8h": 28800}
        if operation in durations:
            self.store.mute(node["id"], durations[operation])
            operation = "main"
        await answer()
        await self.show_node(node, panel["topic"], operation)
        return True

    async def poll(self):
        key = f"telegram:{self.id}:offset"
        offset = self.store.state(key, 0)
        while True:
            try:
                updates = await self.call(
                    "getUpdates", offset=offset, timeout=30, allowed_updates=["message", "callback_query"]
                )
                self.poll_healthy = True
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
                self.poll_healthy = False
                log.warning("Telegram updates postponed: %s", type(error).__name__)
                await asyncio.sleep(max(5, getattr(error, "retry", 5)))

    async def close(self):
        tasks = [task for task in (self.task, self.refresh_task) if task]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.session:
            await self.session.close()
        self.geo.close()
