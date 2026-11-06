import asyncio
import hashlib
import ipaddress
import json
import logging
import re
import time

from ..model import Event

AUTH = re.compile(
    r"\b(Failed|Accepted) (password|publickey|keyboard-interactive(?:/pam)?) for (?:invalid user )?(.*?) from (\S+) port (\d+)"
)
BAN = re.compile(r"\[([^\]]+)\]\s+(Ban|Unban)\s+(\S+)")
log = logging.getLogger(__name__)


def parse_record(record):
    message = record.get("MESSAGE", "")
    if not isinstance(message, str):
        return []
    identity = record.get("SYSLOG_IDENTIFIER", "")
    comm = record.get("_COMM", "")
    fields = None
    if identity in ("sshd", "sshd-session") or comm in ("sshd", "sshd-session"):
        match = AUTH.search(message)
        if match:
            state, method, user, ip, port = match.groups()
            fields = dict(
                kind="auth.success" if state == "Accepted" else "auth.failure",
                ip=ip,
                user=user[:128],
                method=method,
                port=int(port),
                service="ssh",
            )
    elif (
        identity.startswith("fail2ban")
        or comm.startswith("fail2ban")
        or record.get("_SYSTEMD_UNIT") == "fail2ban.service"
    ):
        match = BAN.search(message)
        if match:
            jail, action, ip = match.groups()
            fields = dict(
                kind="access.ban" if action == "Ban" else "access.unban",
                ip=ip,
                service=jail[:64],
                method="fail2ban",
            )
    if fields is None:
        return []
    try:
        ipaddress.ip_address(fields["ip"])
        timestamp = int(record.get("__REALTIME_TIMESTAMP", int(time.time() * 1e6))) / 1e6
        cursor = record.get("__CURSOR", json.dumps(record, sort_keys=True))
        event = Event(hashlib.sha256(cursor.encode()).hexdigest(), time=timestamp, **fields)
        return [Event.parse(event.wire())]
    except (ValueError, TypeError):
        return []


class JournalSeekError(RuntimeError):
    pass


class JournalSource:
    def __init__(self, config):
        self.config = config

    @staticmethod
    def checkpoint(record):
        return json.dumps(
            {
                "cursor": record["__CURSOR"],
                "time_us": int(record.get("__REALTIME_TIMESTAMP", time.time() * 1e6)),
            }
        )

    async def records(self, cursor):
        checkpoint = json.loads(cursor) if cursor and cursor.startswith("{") else {"cursor": cursor}
        raw_cursor = checkpoint.get("cursor")
        since = max(
            time.time() - 7 * 86400, checkpoint.get("time_us", (time.time() - 7 * 86400) * 1e6) / 1e6 - 1
        )
        capture = None
        try:
            if self.config.get("password_capture"):
                from .passwords import PasswordCapture

                capture = PasswordCapture(self.config.get("password_socket", "/run/gatewatch/passwords.sock"))
                await capture.start()
            while True:
                command = ["journalctl", "--follow", "--output=json", "--no-pager", "--all"]
                if raw_cursor:
                    command += ["--after-cursor=" + raw_cursor]
                else:
                    command += ["--since=" + ("@" + str(int(since)) if cursor else "now")]
                command += [
                    "SYSLOG_IDENTIFIER=sshd",
                    "+",
                    "SYSLOG_IDENTIFIER=sshd-session",
                    "+",
                    "_COMM=sshd",
                    "+",
                    "_COMM=sshd-session",
                    "+",
                    "_SYSTEMD_UNIT=fail2ban.service",
                ]
                try:
                    async for position, events in self.stream(command, capture):
                        yield position, events
                    return
                except JournalSeekError:
                    if not raw_cursor:
                        raise
                    log.warning("Saved journal cursor expired; resuming from available timestamp")
                    raw_cursor = None
        finally:
            if capture:
                await capture.close()

    async def stream(self, command, capture):
        process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=1024 * 1024
        )
        seek_failed = False

        async def drain():
            nonlocal seek_failed
            async for line in process.stderr:
                message = line.decode(errors="replace").strip()
                if "cursor" in message.lower() and any(
                    word in message.lower() for word in ("seek", "invalid", "failed")
                ):
                    seek_failed = True
                log.warning("journalctl: %s", message[:300])

        stderr = asyncio.create_task(drain())
        cursor_pending = None
        events_pending = []
        pending_since = time.monotonic()
        line_task = asyncio.create_task(process.stdout.readline())
        try:
            while True:
                timeout = max(0, 0.25 - (time.monotonic() - pending_since)) if cursor_pending else None
                done, _ = await asyncio.wait({line_task}, timeout=timeout)
                if not done:
                    yield cursor_pending, events_pending
                    cursor_pending, events_pending = None, []
                    continue
                line = line_task.result()
                if not line:
                    break
                line_task = asyncio.create_task(process.stdout.readline())
                try:
                    record = json.loads(line)
                    if record.get("__CURSOR"):
                        if not cursor_pending:
                            pending_since = time.monotonic()
                        cursor_pending = self.checkpoint(record)
                        events = parse_record(record)
                        if capture:
                            events = [await capture.apply(record, event) for event in events]
                        events_pending.extend(events)
                    if len(events_pending) >= 64:
                        yield cursor_pending, events_pending
                        cursor_pending, events_pending = None, []
                except (ValueError, TypeError, AttributeError):
                    log.warning("Skipped malformed journal record")
            if cursor_pending:
                yield cursor_pending, events_pending
            await process.wait()
            await stderr
            if seek_failed:
                raise JournalSeekError("Journal cursor unavailable")
            if process.returncode:
                raise RuntimeError("journalctl exited; check journal access")
        finally:
            line_task.cancel()
            await asyncio.gather(line_task, return_exceptions=True)
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 5)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
            await stderr
