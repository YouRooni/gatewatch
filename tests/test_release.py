import io
import json
import sqlite3
import tarfile
import tempfile
import unittest
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from gatewatch.config import validate
from gatewatch.database import APPLICATIONS, connect, schema, script
from gatewatch.files import write_json
from gatewatch.installation import Layout, Manager, Redirects, safe_extract, verify_checksum, version_key
from gatewatch.sources.journal import JournalSeekError, JournalSource


class ReleaseTest(unittest.TestCase):
    def test_partial_install_reuses_identical_config(self):
        with tempfile.TemporaryDirectory() as temp:
            layout = Layout(Path(temp))
            layout.config.mkdir(parents=True)
            source = Path(temp) / "agent.json"
            source.write_text("{}")
            manager = Manager(layout, no_start=True)
            with (
                patch.object(manager, "provision", return_value=SimpleNamespace(pw_gid=1)),
                patch.object(manager, "activate"),
                patch.object(manager, "run", return_value=SimpleNamespace(returncode=1)),
                patch.object(manager, "build", side_effect=[RuntimeError("temporary failure"), Path(temp)]),
                patch("gatewatch.installation.os.chown", create=True),
            ):
                with self.assertRaisesRegex(RuntimeError, "temporary failure"):
                    manager.install(Path(temp), "agent", config=source)
                manager.install(Path(temp), "agent", config=source)
            self.assertEqual(layout.config_for("agent").read_text(), "{}")

    def test_adding_role_keeps_installed_plugin_packages(self):
        with tempfile.TemporaryDirectory() as temp:
            layout = Layout(Path(temp))
            layout.config.mkdir(parents=True)
            layout.units.mkdir(parents=True)
            layout.config_for("hub").write_text("{}")
            layout.config_for("agent").write_text("{}")
            (layout.units / "gatewatch-hub.service").write_text("existing")
            layout.save_state({"version": "1.0.0", "packages": ["custom-adapter==1.2.3"]})
            manager = Manager(layout, no_start=True)
            with (
                patch.object(manager, "provision"),
                patch.object(manager, "activate"),
                patch.object(manager, "run", return_value=SimpleNamespace(returncode=1)),
                patch.object(manager, "build", return_value=Path(temp) / "release") as build,
            ):
                manager.install(Path(temp), "agent")
            build.assert_called_once_with(Path(temp), ["custom-adapter==1.2.3"])

    def test_failed_activation_restores_previous_binary_and_units(self):
        with tempfile.TemporaryDirectory() as temp:
            layout = Layout(Path(temp))
            for path in (layout.config, layout.backups, layout.units, layout.releases):
                path.mkdir(parents=True, exist_ok=True)
            old = layout.releases / "old/venv"
            old.mkdir(parents=True)
            layout.current.symlink_to(old, target_is_directory=True)
            layout.config_for("agent").write_text(json.dumps({"database": str(layout.data / "agent.db")}))
            unit = layout.units / "gatewatch-agent.service"
            unit.write_text("old unit")
            layout.save_state({"version": "1.0.0"})
            release = layout.releases / "new"
            (release / "source/gatewatch").mkdir(parents=True)
            (release / "venv").mkdir()
            (release / "source/gatewatch/__init__.py").write_text('__version__ = "1.0.1"')
            (release / "source/deploy").mkdir()
            (release / "source/deploy/gatewatch-agent.service").write_text("new unit")
            manager = Manager(layout, no_start=True)
            with (
                patch.object(manager, "run", return_value=SimpleNamespace(returncode=0)),
                patch.object(layout, "save_state", side_effect=OSError("disk full")),
                self.assertRaises(OSError),
            ):
                manager.activate(release, ["agent"])
            self.assertEqual(layout.current.resolve(), old.resolve())
            self.assertEqual(unit.read_text(), "old unit")
            self.assertEqual(layout.load_state()["version"], "1.0.0")

    def test_config_rejects_future_version_and_boolean_numbers(self):
        config = {
            "role": "agent",
            "database": "agent.db",
            "endpoint": "https://example.org/v1/events",
            "token": "x" * 32,
            "node_id": "a" * 16,
        }
        self.assertEqual(validate(dict(config)), config)
        for change in (
            {"config_version": 2},
            {"heartbeat_seconds": True},
            {"queue_limit": 0},
            {"sources": {"file": {"type": "file", "path": "/log", "poll_seconds": float("nan")}}},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate({**config, **change})

    def test_atomic_secret_preserves_existing_content_on_exclusive_write(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            write_json(path, {"first": True}, exclusive=True)
            with self.assertRaises(FileExistsError):
                write_json(path, {"second": True}, exclusive=True)
            self.assertEqual(json.loads(path.read_text()), {"first": True})
            write_json(path, {"second": True})
            self.assertEqual(json.loads(path.read_text()), {"second": True})

    def test_schema_migration_is_atomic_and_legacy_backup_exists(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "data.db"
            old = sqlite3.connect(path)
            old.execute("CREATE TABLE preserved(value)")
            old.execute("INSERT INTO preserved VALUES ('queued')")
            old.commit()
            old.close()
            db = connect(path, "agent")
            with self.assertRaises(sqlite3.OperationalError), schema(db, "agent"):
                script(db, "CREATE TABLE partial(value); INSERT INTO missing VALUES(1);")
            db = sqlite3.connect(path)
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='partial'").fetchone())
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT value FROM preserved").fetchone()[0], "queued")
            db.close()
            self.assertTrue(Path(str(path) + ".pre-schema-1.sqlite").exists())

    def test_future_schema_and_wrong_role_rejected_without_changes(self):
        for version, application in ((99, 0), (1, APPLICATIONS["hub"])):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "data.db"
                db = sqlite3.connect(path)
                db.execute(f"PRAGMA user_version={version}")
                db.execute(f"PRAGMA application_id={application}")
                db.close()
                with self.assertRaises(ValueError):
                    connect(path, "agent")
                db = sqlite3.connect(path)
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], version)
                self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "delete")
                db.close()

    def test_release_archive_rejects_escapes_and_links(self):
        for name, kind in (
            ("../escaped", tarfile.REGTYPE),
            ("/escaped", tarfile.REGTYPE),
            ("root/link", tarfile.SYMTYPE),
            ("root/..\\escaped", tarfile.REGTYPE),
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp:
                archive = Path(temp) / "bundle.tar.gz"
                with tarfile.open(archive, "w:gz") as target:
                    item = tarfile.TarInfo(name)
                    item.type = kind
                    target.addfile(item, io.BytesIO())
                with self.assertRaises(ValueError):
                    safe_extract(archive, Path(temp) / "source")
                self.assertFalse((Path(temp) / "escaped").exists())

    def test_checksum_and_versions(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "bundle"
            path.write_bytes(b"changed")
            with self.assertRaises(ValueError):
                verify_checksum(path, "0" * 64)
        self.assertLess(version_key("1.0.0rc1"), version_key("1.0.0"))
        self.assertLess(version_key("1.9.0"), version_key("1.10.0"))
        for value in ("latest", "1.0", "../../1.0.0"):
            with self.assertRaises(ValueError):
                version_key(value)

    def test_release_redirect_does_not_forward_github_token(self):
        request = urllib.request.Request("https://api.github.com/asset", headers={"Authorization": "secret"})
        redirected = Redirects().redirect_request(request, None, 302, "", {}, "https://objects.example/file")
        self.assertFalse(redirected.has_header("Authorization"))
        with self.assertRaises(ValueError):
            Redirects().redirect_request(request, None, 302, "", {}, "http://objects.example/file")


class JournalRecoveryTest(unittest.IsolatedAsyncioTestCase):
    async def test_expired_cursor_falls_back_to_timestamp_without_resetting_identity(self):
        commands = []

        async def stream(source, command, capture):
            commands.append(command)
            if len(commands) == 1:
                raise JournalSeekError("expired")
            yield "next", []

        source = JournalSource({})
        checkpoint = json.dumps({"cursor": "old-cursor", "time_us": 10000000})
        with (
            patch.object(JournalSource, "stream", stream),
            patch("gatewatch.sources.journal.time.time", return_value=20),
        ):
            records = source.records(checkpoint)
            self.assertEqual(await anext(records), ("next", []))
            await records.aclose()
        self.assertIn("--after-cursor=old-cursor", commands[0])
        self.assertIn("--since=@9", commands[1])
