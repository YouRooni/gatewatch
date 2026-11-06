import asyncio
import ipaddress
import json


class Fail2banActions:
    capabilities = ["ban", "unban"]

    def __init__(self, config):
        self.path = config.get("socket", "/run/gatewatch-actions.sock")

    async def execute(self, job):
        action = job["action"]
        if action not in self.capabilities:
            return {"ok": False, "detail": "Unsupported action"}
        address = str(ipaddress.ip_address(job["ip"]))
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(self.path), 3)
            try:
                writer.write(json.dumps({"action": action, "ip": address}).encode() + b"\n")
                await writer.drain()
                data = await asyncio.wait_for(reader.readline(), 10)
                result = json.loads(data)
                if type(result.get("ok")) is not bool or not isinstance(result.get("detail"), str):
                    raise ValueError("Invalid helper response")
                return {"ok": result["ok"], "detail": result["detail"][:160]}
            finally:
                writer.close()
                await writer.wait_closed()
        except (OSError, asyncio.TimeoutError, ValueError):
            return {"ok": False, "detail": "Fail2ban helper unavailable"}
