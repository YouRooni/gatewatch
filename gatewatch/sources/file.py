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

    async def ready(self):
        with open(self.path, "rb"):
            pass

    async def records(self, cursor):
        saved = json.loads(cursor) if cursor else None
        with open(self.path, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            inode = f"{metadata.st_dev}:{metadata.st_ino}"
            generation = saved.get("generation", "") if saved and saved["inode"] == inode else ""
            discarding = saved.get("discarding", False) if saved and saved["inode"] == inode else False
            if saved and saved["inode"] == inode and saved["offset"] <= metadata.st_size:
                stream.seek(saved["offset"])
            elif not saved:
                stream.seek(0, os.SEEK_END)
            else:
                log.warning("Log rotated beyond saved cursor; resuming available file")
                generation = str(time.time_ns())
            while True:
                records = []
                partial = False
                for _ in range(64):
                    start = stream.tell()
                    line = stream.readline(65537)
                    if not line:
                        break
                    if discarding:
                        discarding = not line.endswith(b"\n")
                        continue
                    if len(line) > 65536:
                        discarding = not line.endswith(b"\n")
                        continue
                    if not line.endswith(b"\n"):
                        stream.seek(start)
                        partial = True
                        break
                    identity = f"{inode}:{generation}:{start}:" if generation else f"{inode}:{start}:"
                    event_cursor = hashlib.sha256(identity.encode() + line).hexdigest()
                    record = {
                        "MESSAGE": line.decode(errors="replace"),
                        "SYSLOG_IDENTIFIER": self.identity,
                        "__CURSOR": event_cursor,
                        "__REALTIME_TIMESTAMP": str(int(time.time() * 1e6)),
                    }
                    records.extend(parse_record(record))
                position = {
                    "inode": inode,
                    "offset": stream.tell(),
                    "generation": generation,
                    "discarding": discarding,
                }
                if position != saved:
                    saved = position
                    yield json.dumps(saved), records
                current_size = os.fstat(stream.fileno()).st_size
                if stream.tell() < current_size and not partial:
                    await asyncio.sleep(0)
                    continue
                current = os.stat(self.path)
                if f"{current.st_dev}:{current.st_ino}" != inode:
                    return
                if current.st_size < stream.tell():
                    stream.seek(0)
                    generation = str(time.time_ns())
                    discarding = False
                    continue
                await asyncio.sleep(self.interval)
