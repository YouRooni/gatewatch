import asyncio
import hashlib
import time

from ..model import Event


class DemoSource:
    def __init__(self, config):
        self.config = config

    async def records(self, cursor):
        if cursor != "complete-v1":
            stamp = time.time()
            events = []
            for index, (kind, user, method) in enumerate([
                ("auth.failure", "root", "password"), ("auth.failure", "admin", "password"),
                ("auth.failure", "root", "password"), ("access.ban", "", "fail2ban"),
                ("auth.success", "demo", "publickey"), ("access.unban", "", "fail2ban")
            ]):
                events.append(Event(hashlib.sha256(f"gatewatch-demo-v1-{index}".encode()).hexdigest(), kind, stamp,
                                    "203.0.113.42", user=user, method=method, port=54321 if user else 0,
                                    service="sshd" if kind.startswith("access.") else "ssh"))
            yield "complete-v1", events
        while True:
            await asyncio.sleep(3600)
