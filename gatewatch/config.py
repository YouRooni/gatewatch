import json
import math
import re
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

CONFIG_VERSION = 1
DEFAULT_EVENTS = {
    "auth.failure",
    "access.ban",
    "access.unban",
    "node.online",
    "node.offline",
    "node.degraded",
    "node.recovered",
}
PLUGIN_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}")


def text(value, name, limit=4096):
    if not isinstance(value, str) or not value or len(value) > limit or any(ord(char) < 32 for char in value):
        raise ValueError(f"{name} must be a nonempty printable string")
    return value


def integer(config, name, default, minimum, maximum):
    value = config.get(name, default)
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")


def boolean(config, name):
    if name in config and type(config[name]) is not bool:
        raise ValueError(f"{name} must be true or false")


def plugin_options(options, name):
    if (
        not isinstance(options, dict)
        or not isinstance(options.get("type"), str)
        or not PLUGIN_NAME.fullmatch(options["type"])
    ):
        raise ValueError(f"{name} requires a valid plugin type")


def validate(config):
    if not isinstance(config, dict) or config.get("role") not in ("hub", "agent"):
        raise ValueError("Config role must be hub or agent")
    if type(config.get("config_version", 1)) is not int or config.get("config_version", 1) != CONFIG_VERSION:
        raise ValueError("Unsupported config_version; upgrade Gatewatch first")
    text(config.get("database"), "database")
    if config["role"] == "hub":
        channels = config.get("channels")
        if not isinstance(channels, dict) or not 1 <= len(channels) <= 16:
            raise ValueError("channels must contain 1..16 named plugins")
        integer(config, "aggregation_seconds", 300, 30, 3600)
        integer(config, "offline_seconds", 180, 90, 86400)
        integer(config, "queue_limit", 100000, 1, 1000000)
        integer(config, "event_limit", 1000000, 1, 100000000)
        integer(config, "port", 8787, 1, 65535)
        text(config.get("listen", "127.0.0.1"), "listen", 256)
        for key, options in channels.items():
            if not isinstance(key, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,32}", key):
                raise ValueError("Invalid channel id")
            plugin_options(options, f"channels.{key}")
            if "events" in options and (
                not isinstance(options["events"], list)
                or any(
                    not isinstance(kind, str) or kind not in DEFAULT_EVENTS | {"auth.success"}
                    for kind in options["events"]
                )
            ):
                raise ValueError("Invalid channel events subscription")
            if options["type"] != "telegram":
                continue
            token = options.get("token")
            if (
                not isinstance(token, str)
                or len(token) > 256
                or not re.fullmatch(r"[0-9]+:[a-zA-Z0-9_-]+", token)
            ):
                raise ValueError("Invalid Telegram bot token")
            if type(options.get("chat_id")) is not int or options["chat_id"] >= 0:
                raise ValueError("Telegram chat_id must be a negative forum group id")
            owners = options.get("owner_ids")
            if (
                not isinstance(owners, list)
                or not 1 <= len(owners) <= 32
                or any(type(owner) is not int or owner <= 0 for owner in owners)
            ):
                raise ValueError("owner_ids must contain positive numeric user ids")
            if options.get("language", "ru") not in ("ru", "en"):
                raise ValueError("Supported languages: ru, en")
            if options.get("time_display", "auto") not in ("auto", "always", "never"):
                raise ValueError("time_display must be auto, always or never")
            try:
                ZoneInfo(text(options.get("timezone", "UTC"), "timezone", 128))
            except (ZoneInfoNotFoundError, ValueError):
                raise ValueError("Invalid timezone") from None
            for name in ("custom_emoji", "rich_messages", "show_node_name"):
                boolean(options, name)
            geo = options.get("geo", {})
            if not isinstance(geo, dict):
                raise ValueError("geo must be an object")
            for name in ("city_database", "asn_database"):
                if name in geo:
                    text(geo[name], f"geo.{name}")
            if geo.get("attribution", "") not in ("", "dbip"):
                raise ValueError("Unknown GeoIP attribution")
    else:
        from .agent import validate_endpoint

        validate_endpoint(text(config.get("endpoint"), "endpoint", 2048))
        token = config.get("token")
        if not isinstance(token, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{32,256}", token):
            raise ValueError("Invalid node token")
        node = config.get("node_id")
        if not isinstance(node, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{16,80}", node):
            raise ValueError("Invalid node id")
        integer(config, "heartbeat_seconds", 30, 10, 60)
        integer(config, "queue_limit", 50000, 1, 1000000)
        sources = config.get("sources", {"journal": {"type": "journal"}})
        if not isinstance(sources, dict) or not 1 <= len(sources) <= 16:
            raise ValueError("sources must contain 1..16 named plugins")
        sockets = set()
        for key, options in sources.items():
            text(key, "source id", 64)
            plugin_options(options, f"sources.{key}")
            if options["type"] == "file":
                text(options.get("path"), "file source path")
                interval = options.get("poll_seconds", 2)
                if (
                    type(interval) not in (float, int)
                    or not math.isfinite(interval)
                    or not 1 <= interval <= 300
                ):
                    raise ValueError("poll_seconds must be between 1 and 300")
            if options["type"] == "journal":
                boolean(options, "password_capture")
                if options.get("password_capture"):
                    path = text(
                        options.get("password_socket", "/run/gatewatch/passwords.sock"),
                        "password_socket",
                        100,
                    )
                    if not Path(path).is_absolute() or path in sockets:
                        raise ValueError("Password capture sockets must be absolute and unique")
                    sockets.add(path)
        if config.get("actions") is not None:
            plugin_options(config["actions"], "actions")
    return config


def read_config(path):
    try:
        with open(path, encoding="utf-8-sig") as source:
            return validate(json.load(source))
    except json.JSONDecodeError as error:
        raise ValueError(f"Invalid config JSON at line {error.lineno}, column {error.colno}") from None
