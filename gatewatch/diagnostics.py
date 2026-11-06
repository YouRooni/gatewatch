import importlib.util
import os
import sqlite3
from pathlib import Path

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
