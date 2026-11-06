from datetime import datetime
from html import escape
import json
import time
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


def password_line(card, language):
    if card.get("password") is None:
        return ""
    label = ("Последний пароль" if card.get("count", 1) > 1 else "Пароль") if language == "ru" else ("Last password" if card.get("count", 1) > 1 else "Password")
    value = json.dumps(card["password"], ensure_ascii=False)[1:-1].replace('\x7f', '\\x7f')
    return f"<b>{label}:</b> <code>{escape(value if value else '(empty)')}</code>"


def password_samples(card):
    if card.get('passwords'):
        return card['passwords']
    return [{'value':card['password'],'count':1}] if card.get('password') is not None else []


def sample_line(sample, preview=False):
    value = json.dumps(sample['value'],ensure_ascii=False)[1:-1].replace('\x7f','\\x7f') or '(empty)'
    if preview and len(value)>64:
        value = value[:64]+'…'
    count = f" · <code>×{sample['count']}</code>" if sample['count']>1 else ''
    return f'<code>{escape(value)}</code>{count}'


def render(node, card, language="ru", timezone="UTC", custom=False, geo="", attribution="", show_node_name=False, time_display="auto"):
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
        if password_line(card, language):
            lines.append(password_line(card, language))
        if card.get("port"):
            lines.append(f"{text['port']}: <code>{card['port']}</code>")
    elif kind.startswith("access."):
        lines += ["", f"{text['source']}: Fail2ban · {escape(card.get('service', ''))}"]
    zone = ZoneInfo(timezone)
    stamp = datetime.fromtimestamp(card["last"], zone).strftime("%d.%m · %H:%M:%S %Z")
    if card.get("count", 1) > 1:
        first = datetime.fromtimestamp(card["first"], zone).strftime("%H:%M:%S")
        stamp += f" · {first}–{datetime.fromtimestamp(card['last'], zone).strftime('%H:%M:%S')}"
    if time_display == "always" or (time_display == "auto" and time.time() - card["last"] > 60):
        lines += ["", f"<i>{escape(stamp)}</i>"]
    elif card.get("count", 1) > 1:
        lines += ["", f"<i>{first}–{datetime.fromtimestamp(card['last'], zone).strftime('%H:%M:%S')}</i>"]
    if geo and attribution == "dbip":
        lines.append('<a href="https://db-ip.com">DB-IP</a>')
    return "\n".join(lines)


def render_rich(node, card, language="ru", timezone="UTC", custom=False, geo="", attribution="", show_node_name=False, time_display="auto"):
    text = TEXT[language]
    kind = card["kind"]
    icon_id, fallback = ICONS[kind]
    icon = f'<tg-emoji emoji-id="{icon_id}">{fallback}</tg-emoji>' if custom else fallback
    count = f" · <code>{card['count']}</code>" if card.get("count", 1) > 1 else ""
    blocks = [f"<p>{icon} <b>{text[kind]}</b>{count}</p>"]
    if show_node_name:
        blocks.append(f"<p><b>{escape(node['name'])}</b></p>")
    ru = language == "ru"
    primary, details = [], []

    def field(label, value):
        return f"<b>{escape(label)}:</b> <code>{escape(str(value))}</code>"

    if card.get("ip"):
        primary.append(field("IP", card["ip"]))
    if kind.startswith("auth."):
        users = ", ".join(user or "?" for user in card.get("users", []))
        primary.append(field(text['users'], users))
        primary.append(field(text['method'], card.get('method', '')))
        samples = password_samples(card)
        if samples:
            primary.append('<b>'+('Пароли:' if ru and len(samples)>1 else 'Пароль:' if ru else 'Passwords:' if len(samples)>1 else 'Password:')+'</b>')
            primary.extend(sample_line(sample,True) for sample in samples[:3])
            long_values = any(len(json.dumps(sample['value'],ensure_ascii=False)[1:-1])>64 for sample in samples[:3])
            extra = samples if long_values else samples[3:]
            if extra:
                details.append('<b>'+('Полные пароли:' if ru and long_values else 'Другие пароли:' if ru else 'Full passwords:' if long_values else 'More passwords:')+'</b>')
                details.extend(sample_line(sample) for sample in extra)
        details.append(field("Сервис" if ru else "Service", card.get('service', 'ssh')))
        if card.get("port"):
            details.append(field(text['port'], card['port']))
    elif kind.startswith("access."):
        primary.append(field(text['source'], "Fail2ban · " + card.get('service', '')))
    if geo:
        parts = geo.split(' · ')
        location = ' · '.join(part for part in parts if not part.startswith('AS'))
        network = ' · '.join(part for part in parts if part.startswith('AS'))
        if location:
            primary.append(field("Локация" if ru else "Location", location))
        if network:
            primary.append(field("Сеть" if ru else "Network", network))
    if primary:
        blocks.append(f"<blockquote>{'<br>'.join(primary)}</blockquote>")
    zone = ZoneInfo(timezone)
    stamp = datetime.fromtimestamp(card["last"], zone).strftime("%d.%m · %H:%M:%S %Z")
    if time_display == "always" or (time_display == "auto" and time.time() - card["last"] > 60):
        details.append(field("Время события" if ru else "Event time", stamp))
    if card.get("count", 1) > 1:
        first = datetime.fromtimestamp(card["first"], zone).strftime("%H:%M:%S")
        last = datetime.fromtimestamp(card["last"], zone).strftime("%H:%M:%S")
        details.append(field("Период" if ru else "Period", f"{first}–{last}"))
    if details:
        summary = "Подробнее" if ru else "Details"
        blocks.append(f"<details><summary>{summary}</summary><blockquote>{'<br>'.join(details)}</blockquote></details>")
    if geo and attribution == "dbip":
        blocks.append('<footer><a href="https://db-ip.com">DB-IP</a></footer>')
    return "".join(blocks)
