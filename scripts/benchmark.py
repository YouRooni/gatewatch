import argparse
import json
import platform
import tempfile
import time
import uuid
from pathlib import Path

from gatewatch.model import Event
from gatewatch.storage import HubStore


def main():
    parser = argparse.ArgumentParser(
        description="Isolated SQLite ingestion benchmark; sends no notifications"
    )
    parser.add_argument("--events", type=int, default=10000)
    args = parser.parse_args()
    if not 100 <= args.events <= 100000:
        parser.error("--events must be 100..100000")
    with tempfile.TemporaryDirectory() as temp:
        store = HubStore(str(Path(temp) / "bench.db"))
        node, _ = store.add_node("Benchmark")
        started = time.monotonic()
        bucket = int(time.time() // 300) * 300 + 1
        for offset in range(0, args.events, 100):
            events = [
                Event(
                    str(uuid.uuid4()),
                    "auth.failure",
                    bucket,
                    f"203.0.113.{index % 100 + 1}",
                    user="root",
                    method="password",
                )
                for index in range(offset, min(offset + 100, args.events))
            ]
            store.ingest(node, events, ["benchmark"])
            store.ingest(node, events, ["benchmark"])
        seconds = time.monotonic() - started
        unique = store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        cards = store.db.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
        total = sum(json.loads(row[0])["count"] for row in store.db.execute("SELECT payload FROM cards"))
        assert unique == args.events == total
        size = sum(file.stat().st_size for file in Path(temp).glob("bench.db*"))
        result = {
            "events": unique,
            "duplicate_retries": args.events,
            "cards": cards,
            "seconds": round(seconds, 3),
            "unique_events_per_second": round(unique / seconds),
            "database_and_wal_bytes": size,
            "platform": platform.system(),
        }
        if platform.system() == "Linux":
            import resource

            result["peak_rss_kib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        store.db.close()
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
