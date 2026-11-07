import ipaddress
import re
import time
import uuid
from dataclasses import asdict, dataclass

KINDS = {"auth.failure", "auth.success", "access.ban", "access.unban", "event"}
EVENT_MAX_AGE = 7 * 86400
EVENT_FUTURE_SKEW = 300


@dataclass(frozen=True)
class Event:
    id: str
    kind: str
    time: float
    ip: str = ""
    user: str = ""
    method: str = ""
    port: int = 0
    service: str = "ssh"
    detail: str = ""
    password: str | None = None
    title: str | None = None
    level: str | None = None

    def wire(self):
        return {key: value for key, value in asdict(self).items() if value is not None}

    @classmethod
    def parse(cls, data, *, allow_expired=False, now=None):
        if not isinstance(data, dict) or set(data) - set(cls.__dataclass_fields__):
            raise ValueError("Unknown event fields")
        event = cls(**data)
        if not isinstance(event.id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{16,80}", event.id):
            raise ValueError("Invalid event id")
        if not isinstance(event.kind, str) or event.kind not in KINDS:
            raise ValueError("Unsupported event kind")
        now = time.time() if now is None else now
        if type(event.time) not in (int, float) or not (
            (0 if allow_expired else now - EVENT_MAX_AGE) <= event.time <= now + EVENT_FUTURE_SKEW
        ):
            raise ValueError("Event time outside seven-day window")
        if not isinstance(event.ip, str):
            raise ValueError("Invalid IP")
        if event.ip or event.kind != "event":
            address = ipaddress.ip_address(event.ip)
            if getattr(address, "scope_id", None):
                raise ValueError("Scoped IP addresses are not supported")
        if type(event.port) is not int or not 0 <= event.port <= 65535:
            raise ValueError("Invalid port")
        for key, limit in (
            ("user", 128),
            ("method", 32),
            ("service", 64),
            ("detail", 1024 if event.kind == "event" else 128),
        ):
            value = getattr(event, key)
            if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 for c in value):
                raise ValueError(f"Invalid {key}")
        if event.kind == "event":
            if (
                not isinstance(event.title, str)
                or not event.title.strip()
                or len(event.title) > 128
                or any(ord(c) < 32 for c in event.title)
                or event.level not in ("info", "warning", "error")
                or not event.service.strip()
            ):
                raise ValueError("Service events require title, service and level (info/warning/error)")
            if event.user or event.method or event.port:
                raise ValueError("Authentication fields are not used in service events")
        elif event.title is not None or event.level is not None:
            raise ValueError("Title and level are reserved for service events")
        if event.password is not None and (
            event.kind != "auth.failure"
            or event.method != "password"
            or not isinstance(event.password, str)
            or len(event.password) > 512
        ):
            raise ValueError("Password capture is limited to failed password authentication")
        return event

    @classmethod
    def new(cls, kind, ip="", **fields):
        return cls(str(uuid.uuid4()), kind, time.time(), ip, **fields)
