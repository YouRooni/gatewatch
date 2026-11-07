import asyncio
import importlib.util
import os
import sqlite3
import time
from pathlib import Path

import aiohttp

from . import PROTOCOL_VERSION, __version__
from .database import check_schema
from .plugins import plugin


def inspect_config(config):
    checks = []

    def add(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    add("config", True, "valid")
    database = Path(config["database"]).resolve()
    if database.exists():
        db = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
        try:
            version, _ = check_schema(db, config["role"])
            add("database", db.execute("PRAGMA quick_check").fetchone()[0] == "ok", f"schema {version}")
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "deliveries" in tables:
                pending, oldest = db.execute(
                    "SELECT COUNT(*),MIN(c.created) FROM deliveries d JOIN cards c ON c.id=d.card WHERE d.dirty=1"
                ).fetchone()
                add("delivery_queue", True, queue_detail(pending, oldest))
                pending_actions = (
                    db.execute("SELECT COUNT(*) FROM actions WHERE result IS NULL").fetchone()[0]
                    if "actions" in tables
                    else 0
                )
                add("pending_actions", True, str(pending_actions))
            if "queue" in tables:
                pending, oldest = db.execute("SELECT COUNT(*),MIN(created) FROM queue").fetchone()
                add("outbox", True, queue_detail(pending, oldest))
            if "outbox_losses" in tables:
                for reason, count in db.execute("SELECT reason,count FROM outbox_losses"):
                    add(f"outbox_loss:{reason}", True, str(count))
        except (ValueError, sqlite3.Error) as error:
            add("database", False, str(error))
        finally:
            db.close()
    else:
        parent = database.parent
        while not parent.exists():
            parent = parent.parent
        add("database", os.access(parent, os.W_OK), "will be created")
    if config["role"] == "hub":
        entries = [("channels", key, options) for key, options in config["channels"].items()]
    else:
        entries = [
            ("sources", key, options)
            for key, options in config.get("sources", {"journal": {"type": "journal"}}).items()
        ]
        if config.get("actions"):
            entries.append(("actions", "actions", config["actions"]))
    for group, key, options in entries:
        try:
            plugin(group, options["type"])
            add(f"plugin:{key}", True, options["type"])
        except (ImportError, ValueError) as error:
            add(f"plugin:{key}", False, type(error).__name__)
        if options["type"] == "file":
            add(
                f"file:{key}",
                os.access(options["path"], os.R_OK),
                "readable" if os.access(options["path"], os.R_OK) else "not readable",
            )
        geo = options.get("geo", {})
        if geo.get("city_database") or geo.get("asn_database"):
            add("geoip2", importlib.util.find_spec("geoip2") is not None, "optional dependency")
            for name in ("city_database", "asn_database"):
                if geo.get(name):
                    add(name, os.access(geo[name], os.R_OK), "database file")
    return {
        "version": __version__,
        "protocol": PROTOCOL_VERSION,
        "role": config["role"],
        "ok": all(check["ok"] for check in checks),
        "checks": checks,
    }


def queue_detail(pending, oldest):
    age = max(0, int(time.time() - oldest)) if oldest is not None else 0
    return f"{pending} pending; oldest {age}s"


async def inspect_online(config, result):
    from .agent import validate_endpoint

    async def request(url, **options):
        try:
            async with session.get(url, allow_redirects=False, **options) as response:
                return response.status
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return 0

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as session:
        if config["role"] == "hub":
            host = config.get("listen", "127.0.0.1")
            if host in ("0.0.0.0", "::"):
                host = "::1" if host == "::" else "127.0.0.1"
            host = f"[{host}]" if ":" in host else host
            code = await request(f"http://{host}:{config.get('port', 8787)}/readyz")
            name = "hub_readiness"
        else:
            endpoint = validate_endpoint(config["endpoint"])
            code = await request(
                endpoint + "/v1/probe",
                headers={"Authorization": "Bearer " + config["token"]},
                params={"node_id": config["node_id"]},
            )
            name = "hub_authentication"
    detail = {
        200: "ready",
        401: "identity rejected",
        404: "online probe unavailable on this hub",
        503: "degraded",
    }
    result["checks"].append({"name": name, "ok": code == 200, "detail": detail.get(code, "unavailable")})
    result["ok"] = result["ok"] and code == 200
    return result
