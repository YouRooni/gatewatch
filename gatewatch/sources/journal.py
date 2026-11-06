import asyncio
import hashlib
import ipaddress
import json
import logging
import re
import time

from ..model import Event

AUTH = re.compile(r"\b(Failed|Accepted) (password|publickey|keyboard-interactive(?:/pam)?) for (?:invalid user )?(.*?) from (\S+) port (\d+)")
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
            fields = dict(kind="auth.success" if state == "Accepted" else "auth.failure", ip=ip,
                          user=user[:128], method=method, port=int(port), service="ssh")
    elif identity.startswith("fail2ban") or comm.startswith("fail2ban") or record.get("_SYSTEMD_UNIT") == "fail2ban.service":
        match = BAN.search(message)
        if match:
            jail, action, ip = match.groups()
            fields = dict(kind="access.ban" if action == "Ban" else "access.unban", ip=ip, service=jail[:64], method="fail2ban")
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


class JournalSource:
    def __init__(self, config):
        self.config = config

    async def records(self, cursor):
        command = ["journalctl", "--follow", "--output=json", "--no-pager", "--all"]
        if cursor:
            command += ["--after-cursor=" + cursor]
        else:
            command += ["--since=now"]
        command += ["SYSLOG_IDENTIFIER=sshd", "+", "SYSLOG_IDENTIFIER=sshd-session", "+",
                    "_COMM=sshd", "+", "_COMM=sshd-session", "+", "_SYSTEMD_UNIT=fail2ban.service"]
        process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE,
                                                       stderr=asyncio.subprocess.PIPE, limit=1024 * 1024)

        async def drain():
            async for line in process.stderr:
                log.warning("journalctl: %s", line.decode(errors="replace").strip()[:300])

        stderr = asyncio.create_task(drain())
        cursor_pending = None
        events_pending = []
        pending_since = time.monotonic()
        line_task = asyncio.create_task(process.stdout.readline())
        try:
            while True:
                done, _ = await asyncio.wait({line_task}, timeout=max(0, 0.25 - (time.monotonic() - pending_since)) if cursor_pending else None)
                if not done:
                    yield cursor_pending, events_pending
                    cursor_pending, events_pending = None, []
                    pending_since = time.monotonic()
                    continue
                line = line_task.result()
                if not line:
                    break
                line_task = asyncio.create_task(process.stdout.readline())
                try:
                    record = json.loads(line)
                    record_cursor = record.get("__CURSOR")
                    if record_cursor:
                        if not cursor_pending:
                            pending_since = time.monotonic()
                        cursor_pending = record_cursor
                        events_pending.extend(parse_record(record))
                    if len(events_pending) >= 64:
                        yield cursor_pending, events_pending
                        cursor_pending, events_pending = None, []
                        pending_since = time.monotonic()
                except (ValueError, TypeError):
                    log.warning("Skipped malformed journal record")
            if cursor_pending:
                yield cursor_pending, events_pending
            await process.wait()
            if process.returncode:
                raise RuntimeError("journalctl exited; check journal access and cursor retention")
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
