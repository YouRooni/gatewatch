import json
import os
import stat
import tempfile
from pathlib import Path


def sync_directory(path):
    if os.name == "posix":
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def write_json(path, data, exclusive=False):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if exclusive:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as output:
                json.dump(data, output, ensure_ascii=False, indent=2)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        sync_directory(target.parent)
        return
    previous = target.lstat() if target.exists() else None
    if previous and not stat.S_ISREG(previous.st_mode):
        raise ValueError("Refusing to replace a non-regular config file")
    fd, temporary = tempfile.mkstemp(prefix="." + target.name + "-", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as output:
            json.dump(data, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, stat.S_IMODE(previous.st_mode) if previous else 0o600)
        if previous and os.name == "posix" and os.geteuid() == 0:
            os.chown(temporary, previous.st_uid, previous.st_gid)
        os.replace(temporary, target)
        sync_directory(target.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)
