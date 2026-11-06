import asyncio
import hashlib
import json
import logging
import os
import time

from .journal import parse_record

log = logging.getLogger(__name__)


class FileSource:
    def __init__(self, config):
        self.path = config["path"]
        self.interval = max(1, float(config.get("poll_seconds", 2)))
        self.identity = config.get("identity", "fail2ban-server")

    async def records(self, cursor):
        saved = json.loads(cursor) if cursor else None
        with open(self.path, "rb") as stream:
            stat = os.fstat(stream.fileno())
            inode = f"{stat.st_dev}:{stat.st_ino}"
            if saved and saved["inode"] == inode and saved["offset"] <= stat.st_size:
                stream.seek(saved["offset"])
            elif not saved:
                stream.seek(0, os.SEEK_END)
            else:
                log.warning("Log rotated beyond saved cursor; resuming available file")
            while True:
                records = []
                offset = stream.tell()
                for _ in range(64):
                    start = stream.tell()
                    line = stream.readline(65537)
                    if not line:
                        break
                    if not line.endswith(b"\n") and len(line) <= 65536:
                        stream.seek(start)
                        break
                    offset = stream.tell()
                    if len(line) > 65536:
                        continue
                    event_cursor = hashlib.sha256(f"{inode}:{start}:".encode() + line).hexdigest()
                    record = {"MESSAGE": line.decode(errors="replace"), "SYSLOG_IDENTIFIER": self.identity,
                              "__CURSOR": event_cursor, "__REALTIME_TIMESTAMP": str(int(time.time() * 1e6))}
                    records.extend(parse_record(record))
                if offset != (saved or {}).get("offset") or inode != (saved or {}).get("inode"):
                    saved = {"inode": inode, "offset": offset}
                    yield json.dumps(saved), records
                if stream.tell() < os.fstat(stream.fileno()).st_size:
                    await asyncio.sleep(0.05)
                    continue
                try:
                    current = os.stat(self.path)
                    if f"{current.st_dev}:{current.st_ino}" != inode:
                        return
                    if current.st_size < stream.tell():
                        stream.seek(0)
                except FileNotFoundError:
                    pass
                await asyncio.sleep(self.interval)
