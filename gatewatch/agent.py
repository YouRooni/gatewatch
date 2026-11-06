import asyncio
import logging
import random
from urllib.parse import urlsplit

import aiohttp

from .plugins import plugin
from .storage import AgentStore

log = logging.getLogger(__name__)


def validate_endpoint(endpoint):
    url = urlsplit(endpoint)
    if url.username or url.password or url.query or url.fragment:
        raise ValueError("Endpoint must not contain credentials, query or fragment")
    if not url.hostname or (url.scheme != "https" and not (url.scheme == "http" and url.hostname in ("localhost", "127.0.0.1", "::1"))):
        raise ValueError("HTTPS endpoint required; HTTP allowed only on loopback")
    return endpoint.rstrip("/")


async def run(config):
    endpoint = validate_endpoint(config["endpoint"]) + "/v1/events"
    store = AgentStore(config["database"], config.get("queue_limit", 50000))
    wake = asyncio.Event()

    async def collect(source_id, options):
        source = plugin("sources", options["type"])(options)
        while True:
            try:
                async for cursor, events in source.records(store.cursor(source_id)):
                    store.append(source_id, cursor, events)
                    if events:
                        wake.set()
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log.warning("Source %s paused: %s", source_id, type(error).__name__)
                await asyncio.sleep(10)

    async def send():
        delay = 1
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25)) as session:
            while True:
                wake.clear()
                batch = store.batch()
                try:
                    async with session.post(endpoint, json={"version": 1, "node_id": config["node_id"], "events": batch},
                                            headers={"Authorization": "Bearer " + config["token"]}, allow_redirects=False) as response:
                        if response.status != 200:
                            raise RuntimeError(f"HTTP {response.status}")
                        result = await response.json()
                        if result.get("accepted") != len(batch):
                            raise RuntimeError("Invalid acknowledgement")
                    store.ack(batch)
                    delay = 1
                    if len(batch) == 100:
                        await asyncio.sleep(0.2)
                        continue
                    try:
                        await asyncio.wait_for(wake.wait(), config.get("heartbeat_seconds", 30))
                        await asyncio.sleep(1)
                    except asyncio.TimeoutError:
                        pass
                except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError, ValueError) as error:
                    log.warning("Delivery postponed: %s", str(error) if isinstance(error, RuntimeError) else type(error).__name__)
                    await asyncio.sleep(delay + random.uniform(0, delay / 4))
                    delay = min(delay * 2, 60)

    tasks = [asyncio.create_task(collect(key, value)) for key, value in config.get("sources", {"journal": {"type": "journal"}}).items()]
    tasks.append(asyncio.create_task(send()))
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        store.db.close()
