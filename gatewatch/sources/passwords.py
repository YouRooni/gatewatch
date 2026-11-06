import asyncio
import ipaddress
import os
import socket
import stat
import struct
import time
from collections import deque
from dataclasses import replace
from pathlib import Path


class PasswordCapture:
    def __init__(self, path, ttl=30, capacity=512):
        self.path = Path(path)
        self.ttl = ttl
        self.capacity = capacity
        self.pending = deque()
        self.changed = asyncio.Event()
        self.server = None
        self.reaper = None
        self.clients = set()

    async def start(self):
        if self.path.exists():
            mode = self.path.lstat()
            if not stat.S_ISSOCK(mode.st_mode) or mode.st_uid != os.getuid():
                raise ValueError("Unsafe password capture socket path")
            probe = socket.socket(socket.AF_UNIX)
            probe.settimeout(0.2)
            try:
                probe.connect(str(self.path))
            except ConnectionRefusedError:
                self.path.unlink()
            else:
                raise ValueError("Password capture socket already active")
            finally:
                probe.close()
        self.server = await asyncio.start_unix_server(self.receive, path=str(self.path))
        self.path.chmod(0o600)
        self.reaper = asyncio.create_task(self.reap())

    @staticmethod
    def wipe(item):
        secret = item["secret"]
        secret[:] = b"\0" * len(secret)

    def trim(self):
        while self.pending and (self.pending[0]["expires"] < time.monotonic() or len(self.pending) > self.capacity):
            self.wipe(self.pending.popleft())

    async def reap(self):
        while True:
            self.trim()
            await asyncio.sleep(5)

    async def receive(self, reader, writer):
        task = asyncio.current_task()
        self.clients.add(task)
        try:
            peer = writer.get_extra_info("socket").getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            peer_pid, uid, _ = struct.unpack("3i", peer)
            if uid != 0:
                return
            header = await asyncio.wait_for(reader.readexactly(48), 1)
            magic, *numbers = struct.unpack("!12I", header)
            pids, user_len, host_len, secret_len = numbers[:8], *numbers[8:]
            if magic != 0x47575031 or pids[0] != peer_pid or user_len > 256 or host_len > 64 or secret_len > 512:
                return
            payload = await asyncio.wait_for(reader.readexactly(user_len + host_len + secret_len), 1)
            user = payload[:user_len].decode("utf-8", errors="replace")
            host = str(ipaddress.ip_address(payload[user_len:user_len + host_len].decode("ascii")))
            secret = bytearray(payload[user_len + host_len:])
            captured = pids[2] + pids[3] / 1e9 if pids[2] else time.time()
            self.pending.append({"pids": set(pids[:2]), "user": user, "host": host, "secret": secret,
                                 "captured": captured, "expires": time.monotonic() + self.ttl})
            self.trim()
            self.changed.set()
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, ValueError, OSError):
            pass
        finally:
            self.clients.discard(task)
            writer.close()
            await writer.wait_closed()

    async def apply(self, record, event):
        if event.method not in ("password", "keyboard-interactive/pam"):
            return event
        try:
            pid = int(record.get("_PID", 0))
            address = str(ipaddress.ip_address(event.ip))
        except (ValueError, TypeError):
            return event
        for attempt in range(2):
            self.changed.clear()
            self.trim()
            for item in list(self.pending):
                if pid in item["pids"] and item["user"] == event.user and item["host"] == address and item["captured"] <= event.time <= item["captured"] + self.ttl:
                    self.pending.remove(item)
                    password = item["secret"].decode("utf-8", errors="replace")
                    self.wipe(item)
                    if event.kind == "auth.failure" and event.method == "password":
                        return replace(event, password=password)
                    return event
            if attempt == 0:
                try:
                    await asyncio.wait_for(self.changed.wait(), 0.05)
                except asyncio.TimeoutError:
                    break
        return event

    async def close(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            if self.path.exists():
                self.path.unlink()
        if self.reaper:
            self.reaper.cancel()
            await asyncio.gather(self.reaper, return_exceptions=True)
        for client in list(self.clients):
            client.cancel()
        await asyncio.gather(*self.clients, return_exceptions=True)
        while self.pending:
            self.wipe(self.pending.popleft())
