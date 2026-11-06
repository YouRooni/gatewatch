from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

ICONS = {
    "auth.failure": ("5253864872780769235", "❗️"),
    "auth.success": ("5255813619702049821", "✅"),
    "access.ban": ("5253647062104287098", "🔒"),
    "access.unban": ("5253617001628181935", "👌"),
    "node.offline": ("5253864872780769235", "❗️"),
    "node.online": ("5255813619702049821", "✅"),
}
TEXT = {
    "ru": {"auth.failure": "Неудачный вход", "auth.success": "Успешный вход", "access.ban": "IP заблокирован",
           "access.unban": "IP разблокирован", "node.offline": "Нет связи с агентом", "node.online": "Агент на связи",
           "attempts": "Попыток", "users": "Логины", "method": "Метод", "source": "Источник", "port": "Порт клиента"},
    "en": {"auth.failure": "Failed sign-in", "auth.success": "Successful sign-in", "access.ban": "IP banned",
           "access.unban": "IP unbanned", "node.offline": "Agent unreachable", "node.online": "Agent connected",
           "attempts": "Attempts", "users": "Users", "method": "Method", "source": "Source", "port": "Client port"},
}


def render(node, card, language="ru", timezone="UTC", custom=False, geo="", attribution="", show_node_name=False):
    text = TEXT[language]
    kind = card["kind"]
    icon_id, fallback = ICONS[kind]
    icon = f'<tg-emoji emoji-id="{icon_id}">{fallback}</tg-emoji>' if custom else fallback
    lines = [f"{icon} <b>{text[kind]}</b>"]
    if show_node_name:
        lines.append(f"<b>{escape(node['name'])}</b>")
    if card.get("ip"):
        lines.append(f"<code>{escape(card['ip'])}</code>")
    if geo:
        lines.append(escape(geo))
    if kind.startswith("auth."):
        users = ", ".join(escape(user or "?") for user in card.get("users", []))
        lines += ["", f"{text['users']}: <code>{users}</code>"]
        if card.get("count", 1) > 1:
            lines.append(f"{text['attempts']}: <b>{card['count']}</b>")
        lines.append(f"{text['method']}: {escape(card.get('method', ''))}")
        if card.get("port"):
            lines.append(f"{text['port']}: <code>{card['port']}</code>")
    elif kind.startswith("access."):
        lines += ["", f"{text['source']}: Fail2ban · {escape(card.get('service', ''))}"]
    zone = ZoneInfo(timezone)
    stamp = datetime.fromtimestamp(card["last"], zone).strftime("%d.%m · %H:%M:%S %Z")
    if card.get("count", 1) > 1:
        first = datetime.fromtimestamp(card["first"], zone).strftime("%H:%M:%S")
        stamp += f" · {first}–{datetime.fromtimestamp(card['last'], zone).strftime('%H:%M:%S')}"
    lines += ["", f"<i>{escape(stamp)}</i>"]
    if geo and attribution == "dbip":
        lines.append('<a href="https://db-ip.com">DB-IP</a>')
    return "\n".join(lines)


def render_rich(node, card, language="ru", timezone="UTC", custom=False, geo="", attribution="", show_node_name=False):
    text = TEXT[language]
    kind = card["kind"]
    icon_id, fallback = ICONS[kind]
    icon = f'<tg-emoji emoji-id="{icon_id}">{fallback}</tg-emoji>' if custom else fallback
    count = f" · {card['count']}" if card.get("count", 1) > 1 else ""
    blocks = [f"<p>{icon} <b>{text[kind]}</b>{count}</p>"]
    if show_node_name:
        blocks.append(f"<p><b>{escape(node['name'])}</b></p>")
    if card.get("ip"):
        identity = f"<code>{escape(card['ip'])}</code>"
        users = card.get("users", [])
        if len(users) == 1 and users[0]:
            identity += f" · <code>{escape(users[0][:48])}</code>"
        blocks.append(f"<p>{identity}</p>")
    if geo:
        blocks.append(f"<p>{escape(geo.split(' · ')[0])}</p>")
    details = []
    ru = language == "ru"
    if kind.startswith("auth."):
        users = ", ".join(escape(user or "?") for user in card.get("users", []))
        details.append(f"{text['users']}: <code>{users}</code>")
        details.append(f"{text['method']}: {escape(card.get('method', ''))}")
        details.append(("Сервис" if ru else "Service") + f": {escape(card.get('service', 'ssh'))}")
        if card.get("port"):
            details.append(f"{text['port']}: <code>{card['port']}</code>")
    elif kind.startswith("access."):
        details.append(f"{text['source']}: Fail2ban · {escape(card.get('service', ''))}")
    if geo and " · " in geo:
        details.append(escape(geo))
    zone = ZoneInfo(timezone)
    stamp = datetime.fromtimestamp(card["last"], zone).strftime("%d.%m · %H:%M:%S %Z")
    if card.get("count", 1) > 1:
        first = datetime.fromtimestamp(card["first"], zone).strftime("%H:%M:%S")
        last = datetime.fromtimestamp(card["last"], zone).strftime("%H:%M:%S")
        details.append(("Период" if ru else "Period") + f": {first}–{last}")
    if details:
        summary = "Подробнее" if ru else "Details"
        blocks.append(f"<details><summary>{summary}</summary><p>{'<br>'.join(details)}</p></details>")
    credit = ' · <a href="https://db-ip.com">DB-IP</a>' if geo and attribution == "dbip" else ""
    blocks.append(f"<footer>{escape(stamp)}{credit}</footer>")
    return "".join(blocks)
