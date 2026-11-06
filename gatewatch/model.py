import ipaddress
import re
import time
import uuid
from dataclasses import asdict, dataclass

KINDS = {"auth.failure", "auth.success", "access.ban", "access.unban"}


@dataclass(frozen=True)
class Event:
    id: str
    kind: str
    time: float
    ip: str
    user: str = ""
    method: str = ""
    port: int = 0
    service: str = "ssh"
    detail: str = ""

    def wire(self):
        return asdict(self)

    @classmethod
    def parse(cls, data):
        if not isinstance(data, dict) or set(data) - set(cls.__dataclass_fields__):
            raise ValueError("Unknown event fields")
        event = cls(**data)
        if not isinstance(event.id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{16,80}", event.id):
            raise ValueError("Invalid event id")
        if event.kind not in KINDS:
            raise ValueError("Unsupported event kind")
        if type(event.time) not in (int, float) or not time.time() - 7 * 86400 <= event.time <= time.time() + 300:
            raise ValueError("Event time outside seven-day window")
        if not isinstance(event.ip, str):
            raise ValueError("Invalid IP")
        ipaddress.ip_address(event.ip)
        if type(event.port) is not int or not 0 <= event.port <= 65535:
            raise ValueError("Invalid port")
        for key, limit in (("user", 128), ("method", 32), ("service", 64), ("detail", 128)):
            value = getattr(event, key)
            if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 for c in value):
                raise ValueError(f"Invalid {key}")
        return event

    @classmethod
    def new(cls, kind, ip, **fields):
        return cls(str(uuid.uuid4()), kind, time.time(), ip, **fields)
