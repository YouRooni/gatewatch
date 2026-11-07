import json
import time
from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

from .flags import country_flag

ICONS = {
    "event": ("5255813619702049821", "ℹ️"),
    "auth.failure": ("5253864872780769235", "❗️"),
    "auth.success": ("5255813619702049821", "✅"),
    "access.ban": ("5253647062104287098", "🔒"),
    "access.unban": ("5253617001628181935", "👌"),
    "node.offline": ("5253864872780769235", "❗️"),
    "node.online": ("5255813619702049821", "✅"),
    "node.degraded": ("5253864872780769235", "❗️"),
    "node.recovered": ("5255813619702049821", "✅"),
}
TEXT = {
    "ru": {
        "auth.failure": "Неудачный вход",
        "auth.success": "Успешный вход",
        "access.ban": "IP заблокирован",
        "access.unban": "IP разблокирован",
        "node.offline": "Нет связи с агентом",
        "node.online": "Агент на связи",
        "attempts": "Попыток",
        "users": "Логины",
        "method": "Метод",
        "source": "Источник",
        "port": "Порт клиента",
    },
    "en": {
        "auth.failure": "Failed sign-in",
        "auth.success": "Successful sign-in",
        "access.ban": "IP banned",
        "access.unban": "IP unbanned",
        "node.offline": "Agent unreachable",
        "node.online": "Agent connected",
        "attempts": "Attempts",
        "users": "Users",
        "method": "Method",
        "source": "Source",
        "port": "Client port",
    },
}
TEXT["ru"].update({"node.degraded": "Источник недоступен", "node.recovered": "Сбор событий восстановлен"})
TEXT["en"].update({"node.degraded": "Source unavailable", "node.recovered": "Event collection restored"})


def heading(card, text, custom):
    kind = card["kind"]
    icon_id, fallback = ICONS[
        "auth.failure" if kind == "event" and card.get("level") in ("warning", "error") else kind
    ]
    if kind == "event" and card.get("level") == "info":
        icon_id, fallback = "", "ℹ️"
    icon = f'<tg-emoji emoji-id="{icon_id}">{fallback}</tg-emoji>' if custom and icon_id else fallback
    return f"{icon} <b>{escape(card['title'] if kind == 'event' else text[kind])}</b>"


def password_samples(card):
    if card.get("passwords"):
        return card["passwords"]
    return [{"value": card["password"], "count": 1}] if card.get("password") is not None else []


def field(label, value, prefix=""):
    return f"<b>{escape(label)}:</b> {prefix}<code>{escape(str(value))}</code>"


def sample_line(sample, limit=None):
    value = json.dumps(sample["value"], ensure_ascii=False)[1:-1].replace("\x7f", "\\x7f") or "(empty)"
    if limit and len(value) > limit:
        value = value[:limit] + "…"
    count = f" · <code>×{sample['count']}</code>" if sample["count"] > 1 else ""
    return f"<code>{escape(value)}</code>{count}"


def fields(card, language, timezone, custom, geo, time_display, country_code, rich):
    text = TEXT[language]
    ru = language == "ru"
    kind = card["kind"]
    primary, details = [], []
    service_label = "Сервис" if ru else "Service"
    if card.get("ip"):
        primary.append(field("IP", card["ip"]))
    if card.get("sources"):
        primary.append(field("Источники" if ru else "Sources", ", ".join(card["sources"])))
    if kind == "event":
        primary.append(field(service_label, card["service"]))
        if card["level"] != "info":
            primary.append(
                "<b>"
                + {
                    "warning": "Предупреждение" if ru else "Warning",
                    "error": "Ошибка" if ru else "Error",
                }[card["level"]]
                + "</b>"
            )
        if card.get("detail"):
            primary.append(escape(card["detail"]))
    elif kind.startswith("auth."):
        users = ", ".join(user or "?" for user in card.get("users", []))
        if card.get("more_users"):
            users += " …"
        primary.append(field(text["users"], users))
        if card.get("method"):
            primary.append(field(text["method"], card["method"]))
        samples = password_samples(card)
        if samples:
            primary.append("<b>" + ("Пароль" if ru else "Password") + "</b>")
            primary.extend(
                sample_line(sample, 64 if rich or index else 512) for index, sample in enumerate(samples[:3])
            )
            if rich and (
                len(samples) > 3
                or any(len(json.dumps(sample["value"], ensure_ascii=False)) > 66 for sample in samples)
            ):
                details.append(
                    "<b>" + ("Все сохранённые пароли" if ru else "All retained passwords") + "</b>"
                )
                details.extend(sample_line(sample) for sample in samples)
            elif not rich and len(samples) > 3:
                remaining = len(samples) - 3
                primary.append(f"Ещё {remaining}" if ru else f"{remaining} more")
        details.append(field(service_label, card.get("service", "ssh")))
        if card.get("port"):
            details.append(field(text["port"], card["port"]))
    elif kind.startswith("access."):
        primary.append(field(text["source"], "Fail2ban · " + card.get("service", "")))
    if geo:
        parts = geo.split(" · ")
        location = " · ".join(part for part in parts if not part.startswith("AS"))
        network = " · ".join(part for part in parts if part.startswith("AS"))
        if location:
            flag = country_flag(country_code, custom)
            primary.append(field("Локация" if ru else "Location", location, flag + " " if flag else ""))
        if network:
            primary.append(field("Сеть" if ru else "Network", network))
    zone = ZoneInfo(timezone)
    last = datetime.fromtimestamp(card["last"], zone)
    if time_display == "always" or (time_display == "auto" and time.time() - card["last"] > 60):
        details.append(field("Время события" if ru else "Event time", last.strftime("%d.%m · %H:%M:%S %Z")))
    if card.get("count", 1) > 1:
        first = datetime.fromtimestamp(card["first"], zone)
        fmt = "%H:%M:%S" if first.date() == last.date() else "%d.%m %H:%M:%S"
        primary.append(field("Период" if ru else "Period", f"{first:{fmt}}–{last:{fmt}} {last:%Z}"))
    return primary, details


def render(
    node,
    card,
    language="ru",
    timezone="UTC",
    custom=False,
    geo="",
    attribution="",
    show_node_name=False,
    time_display="auto",
    country_code="",
    *,
    rich=False,
):
    header = heading(card, TEXT[language], custom)
    if card.get("count", 1) > 1:
        header += f" · <code>{card['count']}</code>"
    name = f"<b>{escape(node['name'][:128])}</b>" if show_node_name else ""
    primary, details = fields(card, language, timezone, custom, geo, time_display, country_code, rich)
    credit = '<a href="https://db-ip.com">DB-IP</a>' if geo and attribution == "dbip" else ""
    if not rich:
        return "\n".join(part for part in (header, name, *primary, *details, credit) if part)
    blocks = [f"<p>{part}</p>" for part in (header, name) if part]
    if primary:
        blocks.append(f"<blockquote>{'<br>'.join(primary)}</blockquote>")
    if details:
        summary = "Подробнее" if language == "ru" else "Details"
        blocks.append(
            f"<details><summary>{summary}</summary><blockquote>{'<br>'.join(details)}</blockquote></details>"
        )
    if credit:
        blocks.append(f"<footer>{credit}</footer>")
    return "".join(blocks)


def render_rich(*args, **kwargs):
    return render(*args, **kwargs, rich=True)
