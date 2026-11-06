import argparse
import asyncio
import getpass
import json
import logging
import os
import re
import sqlite3
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

from . import __version__
from .storage import HubStore


def write_secret(path, config):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        json.dump(config, output, indent=2, ensure_ascii=False)
        output.write("\n")


def read_config(path):
    with open(path, encoding="utf-8-sig") as source:
        config = json.load(source)
    if not isinstance(config, dict) or config.get("role") not in ("hub", "agent"):
        raise ValueError("Config role must be hub or agent")
    if not isinstance(config.get("database"), str):
        raise ValueError("Database path required")
    if config["role"] == "hub":
        if not config.get("channels"):
            raise ValueError("At least one channel required")
        for key, options in config["channels"].items():
            if not re.fullmatch(r"[a-zA-Z0-9_-]{1,32}", key):
                raise ValueError("Invalid channel id")
            if options["type"] == "telegram":
                if not re.fullmatch(r"[0-9]+:[a-zA-Z0-9_-]+", options.get("token", "")):
                    raise ValueError("Invalid bot token")
                if type(options.get("chat_id")) is not int or not options.get("owner_ids") or any(type(i) is not int or i <= 0 for i in options["owner_ids"]):
                    raise ValueError("Chat id and numeric owner_ids required")
                if options.get("language", "ru") not in ("ru", "en"):
                    raise ValueError("Supported languages: ru, en")
                ZoneInfo(options.get("timezone", "UTC"))
        if not 30 <= config.get("aggregation_seconds", 300) <= 3600:
            raise ValueError("aggregation_seconds must be 30..3600")
        if not 90 <= config.get("offline_seconds", 180) <= 86400:
            raise ValueError("offline_seconds must be 90..86400")
    else:
        from .agent import validate_endpoint
        validate_endpoint(config["endpoint"])
        if not isinstance(config.get("token"), str) or len(config["token"]) < 32:
            raise ValueError("Node token required")
        if not re.fullmatch(r"[a-zA-Z0-9_-]{16,80}", config.get("node_id", "")):
            raise ValueError("Invalid node id")
        if not 10 <= config.get("heartbeat_seconds", 30) <= 60:
            raise ValueError("heartbeat_seconds must be 10..60")
    return config


def parser():
    root = argparse.ArgumentParser(description="Gatewatch — server access notifications")
    root.add_argument("--version", action="version", version=__version__)
    root.add_argument("--config", default="/etc/gatewatch/hub.secret.json")
    sub = root.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="Create hub config; token is prompted securely")
    init.add_argument("--chat-id", type=int)
    init.add_argument("--owner", type=int, action="append")
    init.add_argument("--language", choices=["ru", "en"], default="ru")
    init.add_argument("--timezone", default="UTC")
    init.add_argument("--database", default="/var/lib/gatewatch/hub.db")
    sub.add_parser("run", help="Run hub or agent")
    sub.add_parser("check", help="Validate local config without contacting services")
    geo = sub.add_parser("geo-update", help="Download optional DB-IP Lite city/ASN databases")
    geo.add_argument("--directory", default="/var/lib/gatewatch/geo")
    geo.add_argument("--month")
    nodes = sub.add_parser("nodes", help="Manage node identities").add_subparsers(dest="action", required=True)
    add = nodes.add_parser("add")
    add.add_argument("name")
    add.add_argument("--endpoint", required=True)
    add.add_argument("--out", required=True, help="New agent config file; never overwritten")
    nodes.add_parser("list")
    for action in ("revoke", "remove", "rename", "rotate"):
        command = nodes.add_parser(action)
        command.add_argument("id")
        if action == "rename":
            command.add_argument("name")
        if action == "rotate":
            command.add_argument("--endpoint", required=True)
            command.add_argument("--out", required=True)
    backup = sub.add_parser("backup", help="Consistent SQLite snapshot")
    backup.add_argument("out")
    return root


def node_config(node_id, token, endpoint):
    from .agent import validate_endpoint
    return {"role": "agent", "endpoint": validate_endpoint(endpoint), "node_id": node_id, "token": token,
            "database": "/var/lib/gatewatch/agent.db", "heartbeat_seconds": 30, "queue_limit": 50000,
            "sources": {"journal": {"type": "journal"}}}


def dispatch(args):
    if args.command == "init":
        chat = args.chat_id if args.chat_id is not None else int(input("Telegram group ID (-100…): ").strip())
        owners = args.owner or [int(input("Your Telegram user ID: ").strip())]
        token = getpass.getpass("Bot token: ").strip()
        config = {"role": "hub", "database": args.database, "listen": "127.0.0.1", "port": 8787,
                  "aggregation_seconds": 300, "offline_seconds": 180, "queue_limit": 100000,
                  "channels": {"telegram": {"type": "telegram", "token": token, "chat_id": chat,
                                            "owner_ids": owners, "language": args.language, "timezone": args.timezone,
                                            "custom_emoji": True}}}
        ZoneInfo(args.timezone)
        if not re.fullmatch(r"[0-9]+:[a-zA-Z0-9_-]+", token) or not owners or any(i <= 0 for i in owners) or chat >= 0:
            raise ValueError("Check bot token, negative group ID and positive owner IDs")
        write_secret(args.config, config)
        print("Hub config created. Set up HTTPS before connecting remote nodes.")
        return
    config = read_config(args.config)
    if args.command == "geo-update":
        try:
            from .geo_update import update
            update(args.directory, args.month)
        except ImportError:
            raise ValueError("Install gatewatch[geo] before geo-update") from None
        return
    if args.command == "check":
        print(f"Gatewatch {__version__}: {config['role']} config OK")
        return
    if args.command == "run":
        if config["role"] == "hub":
            from aiohttp import web
            from .hub import create_app
            web.run_app(create_app(config), host=config.get("listen", "127.0.0.1"), port=config.get("port", 8787), access_log=None, print=None)
        else:
            from .agent import run
            asyncio.run(run(config))
        return
    if args.command == "backup":
        destination = Path(args.out)
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        with sqlite3.connect(config["database"]) as source, sqlite3.connect(destination) as target:
            source.backup(target)
        print("Database snapshot created. Back up the secret config separately.")
        return
    if config["role"] != "hub":
        raise ValueError("Node management requires hub config")
    store = HubStore(config["database"])
    try:
        if args.action == "list":
            for node in store.db.execute("SELECT id,name,enabled,seen FROM nodes ORDER BY name"):
                print(f"{node['id']}  {'enabled' if node['enabled'] else 'revoked'}  {node['name']}")
        elif args.action == "add":
            if not args.name.strip() or len(args.name) > 80 or any(ord(c) < 32 for c in args.name):
                raise ValueError("Node name must be 1..80 printable characters")
            from .agent import validate_endpoint
            validate_endpoint(args.endpoint)
            if Path(args.out).exists():
                raise FileExistsError(args.out)
            node_id, token = store.add_node(args.name)
            try:
                write_secret(args.out, node_config(node_id, token, args.endpoint))
            except Exception:
                with store.db:
                    store.db.execute("DELETE FROM nodes WHERE id=?", (node_id,))
                raise
            print(f"Node created: {node_id}\nAgent config: {args.out}")
        else:
            if not store.db.execute("SELECT 1 FROM nodes WHERE id=?", (args.id,)).fetchone():
                raise ValueError("Unknown node ID; use nodes list")
            if args.action == "rotate":
                import secrets
                from .storage import digest
                token = secrets.token_urlsafe(32)
                write_secret(args.out, node_config(args.id, token, args.endpoint))
                with store.db:
                    store.db.execute("UPDATE nodes SET token=?,enabled=1 WHERE id=?", (digest(token), args.id))
                print("Key rotated. Install the new agent config and restart the agent.")
            elif args.action == "rename":
                if not args.name.strip() or len(args.name) > 80 or any(ord(c) < 32 for c in args.name):
                    raise ValueError("Node name must be 1..80 printable characters")
                with store.db:
                    store.db.execute("UPDATE nodes SET name=? WHERE id=?", (args.name, args.id))
                print("Node renamed. Telegram topic name preserved.")
            elif args.action == "revoke":
                with store.db:
                    store.db.execute("UPDATE nodes SET enabled=0 WHERE id=?", (args.id,))
                print("Node key revoked.")
            elif args.action == "remove":
                with store.db:
                    store.db.execute("DELETE FROM deliveries WHERE card IN (SELECT id FROM cards WHERE node=?)", (args.id,))
                    store.db.execute("DELETE FROM cards WHERE node=?", (args.id,))
                    store.db.execute("DELETE FROM events WHERE id LIKE ?", (args.id + ":%",))
                    store.db.execute("DELETE FROM nodes WHERE id=?", (args.id,))
                print("Node and queued events removed. Telegram history preserved.")
    finally:
        store.db.close()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = parser().parse_args()
    try:
        dispatch(args)
    except KeyboardInterrupt:
        pass
    except (ValueError, KeyError, OSError, sqlite3.Error) as error:
        print(f"Gatewatch: {error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
