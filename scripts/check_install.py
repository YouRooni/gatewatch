import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path


def run(*arguments):
    subprocess.run([sys.executable, "scripts/manage.py", "--no-start", "--no-apt", *arguments], check=True)


def main():
    source = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="gatewatch-install-test-") as temp:
        config = Path(temp) / "agent.secret.json"
        config.write_text(
            json.dumps(
                {
                    "role": "agent",
                    "database": "/var/lib/gatewatch/agent.db",
                    "endpoint": "https://example.org/v1/events",
                    "node_id": "a" * 32,
                    "token": "b" * 48,
                    "sources": {"demo": {"type": "demo"}},
                }
            ),
            encoding="utf-8",
        )
        run("install", "agent", "--source", str(source), "--config", str(config))
        previous = Path("/opt/gatewatch").resolve()
        database = sqlite3.connect("/var/lib/gatewatch/agent.db")
        database.execute("CREATE TABLE release_probe(value)")
        database.execute("INSERT INTO release_probe VALUES ('preserved')")
        database.commit()
        database.close()
        following = Path(temp) / "next"
        shutil.copytree(
            source, following, ignore=shutil.ignore_patterns(".git", "tmp", ".venv", "__pycache__")
        )
        version = following / "gatewatch/__init__.py"
        version.write_text(
            version.read_text(encoding="utf-8").replace('"1.0.0"', '"1.0.1"'), encoding="utf-8"
        )
        run("upgrade", "--source", str(following))
        assert Path("/opt/gatewatch").resolve() != previous
        run("rollback")
        assert Path("/opt/gatewatch").resolve() == previous
        database = sqlite3.connect("/var/lib/gatewatch/agent.db")
        assert database.execute("SELECT value FROM release_probe").fetchone()[0] == "preserved"
        database.close()
        run("upgrade", "--source", str(following))
        run("uninstall", "all")
        assert Path("/etc/gatewatch/agent.secret.json").exists()
        assert Path("/var/lib/gatewatch/agent.db").exists()
        assert not Path("/etc/systemd/system/gatewatch-agent.service").exists()
        print("Install, upgrade, rollback and retained-data uninstall passed")


if __name__ == "__main__":
    main()
