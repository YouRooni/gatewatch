import gzip
import os
import re
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


def update(directory, month=None):
    import geoip2.database

    month = month or datetime.now(timezone.utc).strftime("%Y-%m")
    if not re.fullmatch(r"20\d{2}-(0[1-9]|1[0-2])", month):
        raise ValueError("Month must be YYYY-MM")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for kind in ("city", "asn"):
        url = f"https://download.db-ip.com/free/dbip-{kind}-lite-{month}.mmdb.gz"
        fd, name = tempfile.mkstemp(dir=directory, prefix=".download-", suffix=".mmdb")
        try:
            with os.fdopen(fd, "wb") as target:
                with urllib.request.urlopen(url, timeout=60) as response, gzip.GzipFile(fileobj=response) as source:
                    total = 0
                    while chunk := source.read(1024 * 1024):
                        total += len(chunk)
                        if total > 512 * 1024 * 1024:
                            raise ValueError("GeoIP download exceeds 512 MiB")
                        target.write(chunk)
            with geoip2.database.Reader(name) as reader:
                expected = "City" if kind == "city" else "ASN"
                if expected not in reader.metadata().database_type:
                    raise ValueError("Unexpected GeoIP database type")
            os.chmod(name, 0o640)
            os.replace(name, directory / f"dbip-{kind}.mmdb")
        finally:
            if os.path.exists(name):
                os.unlink(name)
    print("DB-IP databases updated. Restart the hub to load them. Data: https://db-ip.com · CC BY 4.0")
