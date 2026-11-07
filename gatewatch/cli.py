import argparse
import asyncio
import getpass
import ipaddress
import json
import logging
import os
import re
import sqlite3
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

from . import __version__
from .config import read_config
from .files import write_json
from .storage import HubStore


def write_secret(path, config):
    write_json(path, config, exclusive=True)


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
    emit = sub.add_parser("emit", help="Queue a service event using an agent config")
    emit.add_argument("title")
    emit.add_argument("--service", required=True)
    emit.add_argument("--level", choices=("info", "warning", "error"), default="info")
    emit.add_argument("--detail", default="")
    emit.add_argument("--ip", default="")
    emit.add_argument("--id")
    doctor = sub.add_parser("doctor", help="Check local database, plugins and source files")
    doctor.add_argument("--json", action="store_true")
    doctor.add_argument("--online", action="store_true", help="Probe hub readiness or node authentication")
    sub.add_parser("migrate", help="Apply supported database migrations with a backup")
    legacy = sub.add_parser("prepare-rollback", help=argparse.SUPPRESS)
    legacy.add_argument("--target", required=True)
    geo = sub.add_parser("geo-update", help="Download optional DB-IP Lite city/ASN databases")
    geo.add_argument("--directory", default="/var/lib/gatewatch/geo")
    geo.add_argument("--month")
    nodes = sub.add_parser("nodes", help="Manage node identities").add_subparsers(
        dest="action", required=True
    )
    add = nodes.add_parser("add")
    add.add_argument("name")
    add.add_argument("--address", required=True, help="Public node IP for its topic title")
    add.add_argument("--endpoint", required=True)
    add.add_argument("--out", required=True, help="New agent config file; never overwritten")
    nodes.add_parser("list")
    for action in ("revoke", "remove", "rename", "rotate", "address"):
        command = nodes.add_parser(action)
        command.add_argument("id")
        if action == "rename":
            command.add_argument("name")
        if action == "address":
            command.add_argument("address")
        if action == "rotate":
            command.add_argument("--endpoint", required=True)
            command.add_argument("--out", required=True)
    backup = sub.add_parser("backup", help="Consistent SQLite snapshot")
    backup.add_argument("out")
    return root


def node_config(node_id, token, endpoint):
    from .agent import validate_endpoint

    return {
        "config_version": 1,
        "role": "agent",
        "endpoint": validate_endpoint(endpoint),
        "node_id": node_id,
        "token": token,
        "database": "/var/lib/gatewatch/agent.db",
        "heartbeat_seconds": 30,
        "queue_limit": 50000,
        "sources": {"journal": {"type": "journal"}},
    }


def dispatch(args):
    if args.command == "init":
        chat = args.chat_id if args.chat_id is not None else int(input("Telegram group ID (-100…): ").strip())
        owners = args.owner or [int(input("Your Telegram user ID: ").strip())]
        token = getpass.getpass("Bot token: ").strip()
        config = {
            "config_version": 1,
            "role": "hub",
            "database": args.database,
            "listen": "127.0.0.1",
            "port": 8787,
            "aggregation_seconds": 300,
            "offline_seconds": 180,
            "queue_limit": 100000,
            "channels": {
                "telegram": {
                    "type": "telegram",
                    "token": token,
                    "chat_id": chat,
                    "owner_ids": owners,
                    "language": args.language,
                    "timezone": args.timezone,
                    "custom_emoji": True,
                }
            },
        }
        ZoneInfo(args.timezone)
        if (
            not re.fullmatch(r"[0-9]+:[a-zA-Z0-9_-]+", token)
            or not owners
            or any(i <= 0 for i in owners)
            or chat >= 0
        ):
            raise ValueError("Check bot token, negative group ID and positive owner IDs")
        write_secret(args.config, config)
        print(f"Hub config created. Telegram language: {args.language}. Timezone: {args.timezone}.")
        print("Set up HTTPS before connecting remote nodes.")
        return
    config = read_config(args.config)
    if args.command == "emit":
        from dataclasses import replace

        from .model import Event
        from .storage import AgentStore

        if config["role"] != "agent":
            raise ValueError("emit requires an agent config")
        event = Event.new(
            "event", args.ip, service=args.service, title=args.title, level=args.level, detail=args.detail
        )
        if args.id:
            event = replace(event, id=args.id)
        event = Event.parse(event.wire())
        store = AgentStore(config["database"], config.get("queue_limit", 50000))
        try:
            store.append(None, None, [event])
        finally:
            store.db.close()
        print(f"Queued: {event.id}")
        return
    if args.command == "prepare-rollback":
        if not args.target.startswith("0.1."):
            raise ValueError("Unsupported legacy rollback target")
        from .database import prepare_legacy

        prepare_legacy(
            config["database"], config["role"], config.get("sources", {"journal": {"type": "journal"}})
        )
        return
    if args.command == "doctor":
        from .diagnostics import inspect_config, inspect_online

        result = inspect_config(config)
        if args.online:
            result = asyncio.run(inspect_online(config, result))
        if args.json:
            print(json.dumps(result))
        else:
            print(f"Gatewatch {__version__} · {config['role']}")
            for check in result["checks"]:
                print(f"{'OK' if check['ok'] else 'FAIL'} {check['name']}: {check['detail']}")
        if not result["ok"]:
            raise SystemExit(1)
        return
    if args.command == "migrate":
        from .database import SCHEMA_VERSION
        from .storage import AgentStore

        store = HubStore(config["database"]) if config["role"] == "hub" else AgentStore(config["database"])
        store.db.close()
        print(f"Database schema {SCHEMA_VERSION} ready")
        return
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

            web.run_app(
                create_app(config),
                host=config.get("listen", "127.0.0.1"),
                port=config.get("port", 8787),
                access_log=None,
                print=None,
            )
        else:
            from .agent import run

            asyncio.run(run(config))
        return
    if args.command == "backup":
        destination = Path(args.out)
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        source = sqlite3.connect(Path(config["database"]).resolve().as_uri() + "?mode=ro", uri=True)
        target = sqlite3.connect(destination)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        print("Database snapshot created. Back up the secret config separately.")
        return
    if config["role"] != "hub":
        raise ValueError("Node management requires hub config")
    store = HubStore(config["database"])
    try:
        if args.action == "list":
            for node in store.db.execute("SELECT id,name,address,enabled,seen FROM nodes ORDER BY name"):
                version = store.state("agent:" + node["id"], {}).get("version", "legacy")
                print(
                    f"{node['id']}  {'enabled' if node['enabled'] else 'revoked'}  {node['name']}  {node['address']}  {version}"
                )
        elif args.action == "add":
            if not args.name.strip() or len(args.name) > 80 or any(ord(c) < 32 for c in args.name):
                raise ValueError("Node name must be 1..80 printable characters")
            from .agent import validate_endpoint

            validate_endpoint(args.endpoint)
            if Path(args.out).exists():
                raise FileExistsError(args.out)
            address = str(ipaddress.ip_address(args.address))
            node_id, token = store.add_node(args.name, address)
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
                    store.db.execute(
                        "UPDATE nodes SET token=?,enabled=1 WHERE id=?", (digest(token), args.id)
                    )
                print("Key rotated. Install the new agent config and restart the agent.")
            elif args.action == "rename":
                if not args.name.strip() or len(args.name) > 80 or any(ord(c) < 32 for c in args.name):
                    raise ValueError("Node name must be 1..80 printable characters")
                with store.db:
                    store.db.execute("UPDATE nodes SET name=? WHERE id=?", (args.name, args.id))
                print("Node renamed. Telegram topic name preserved.")
            elif args.action == "address":
                address = str(ipaddress.ip_address(args.address))
                with store.db:
                    store.db.execute("UPDATE nodes SET address=? WHERE id=?", (address, args.id))
                print("Node address updated. Existing Telegram topic name preserved.")
            elif args.action == "revoke":
                with store.db:
                    store.db.execute("UPDATE nodes SET enabled=0 WHERE id=?", (args.id,))
                print("Node key revoked.")
            elif args.action == "remove":
                with store.db:
                    store.db.execute(
                        "DELETE FROM deliveries WHERE card IN (SELECT id FROM cards WHERE node=?)", (args.id,)
                    )
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
