import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 1
APPLICATIONS = {"hub": 0x47574842, "agent": 0x47574147}


def prepare_legacy(path, role, sources):
    import json

    db = sqlite3.connect(path)
    try:
        with db:
            if role == "hub":
                db.execute(
                    "UPDATE deliveries SET version=(SELECT version FROM cards WHERE cards.id=deliveries.card),dirty=0 WHERE card IN (SELECT id FROM cards WHERE kind IN ('node.degraded','node.recovered'))"
                )
            else:
                for source, options in sources.items():
                    if options.get("type") != "journal":
                        continue
                    row = db.execute("SELECT cursor FROM cursors WHERE source=?", (source,)).fetchone()
                    if row and row[0].startswith("{"):
                        checkpoint = json.loads(row[0])
                        if isinstance(checkpoint.get("cursor"), str):
                            db.execute(
                                "UPDATE cursors SET cursor=? WHERE source=?", (checkpoint["cursor"], source)
                            )
    finally:
        db.close()


def script(db, source):
    statement = ""
    for character in source:
        statement += character
        if character == ";" and sqlite3.complete_statement(statement):
            db.execute(statement)
            statement = ""
    if statement.strip():
        db.execute(statement)


def check_schema(db, role):
    version = db.execute("PRAGMA user_version").fetchone()[0]
    application = db.execute("PRAGMA application_id").fetchone()[0]
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if version > SCHEMA_VERSION:
        raise ValueError("Database was created by a newer Gatewatch version")
    if (
        application not in (0, APPLICATIONS[role])
        or (role == "hub" and "queue" in tables)
        or (role == "agent" and "nodes" in tables)
    ):
        raise ValueError("Database role mismatch")
    return version, tables


def connect(path, role):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=5000")
        version, tables = check_schema(db, role)
        if version == 0 and tables:
            backup = Path(str(path) + ".pre-schema-1.sqlite")
            try:
                fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(fd)
                target = sqlite3.connect(backup)
                try:
                    db.backup(target)
                finally:
                    target.close()
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        return db
    except BaseException:
        db.close()
        raise


@contextmanager
def schema(db, role):
    db.execute("BEGIN IMMEDIATE")
    try:
        check_schema(db, role)
        yield
        db.execute(f"PRAGMA application_id={APPLICATIONS[role]}")
        db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        db.commit()
    except BaseException:
        db.rollback()
        db.close()
        raise
