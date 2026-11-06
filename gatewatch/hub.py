import asyncio
import json
import logging
import random
import time

from aiohttp import web

from .model import Event
from .plugins import plugin
from .storage import HubStore

log = logging.getLogger(__name__)
DEFAULT_EVENTS = {"auth.failure", "access.ban", "access.unban", "node.online", "node.offline"}


def create_app(config, store=None):
    store = store or HubStore(config["database"], config.get("aggregation_seconds", 300), config.get("queue_limit", 100000), config.get("event_limit", 1000000))
    channels = {key: plugin("channels", options["type"])(options, store, key) for key, options in config["channels"].items()}
    subscriptions = {key: set(options.get("events", DEFAULT_EVENTS)) for key, options in config["channels"].items()}
    wake = {key: asyncio.Event() for key in channels}
    app = web.Application(client_max_size=128 * 1024)

    async def ingest(request):
        authorization = request.headers.get("Authorization", "")
        if not authorization.startswith("Bearer ") or len(authorization) > 200:
            raise web.HTTPUnauthorized()
        try:
            data = await request.json()
            if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("node_id"), str):
                raise ValueError("Invalid envelope")
            if not store.authenticate(data["node_id"], authorization[7:]):
                raise web.HTTPUnauthorized()
            batch = data.get("events")
            if not isinstance(batch, list) or len(batch) > 100:
                raise ValueError("Batch must contain at most 100 events")
            events = [Event.parse(event) for event in batch]
            store.ingest(data["node_id"], events, subscriptions)
        except (ValueError, TypeError, KeyError):
            raise web.HTTPBadRequest(text="Invalid event batch") from None
        except OverflowError:
            raise web.HTTPServiceUnavailable(text="Queue full", headers={"Retry-After": "30"}) from None
        for event in wake.values():
            event.set()
        return web.json_response({"accepted": len(events)})

    async def health(request):
        return web.json_response({"status": "ok", "protocol": 1})

    async def worker(key, channel):
        while True:
            wake[key].clear()
            card = store.due(key)
            if not card:
                try:
                    await asyncio.wait_for(wake[key].wait(), 15)
                except asyncio.TimeoutError:
                    pass
                continue
            node = dict(store.db.execute("SELECT * FROM nodes WHERE id=?", (card["node"],)).fetchone())
            node.pop("token", None)
            try:
                if node["muted"] > time.time() and card["kind"] == "auth.failure":
                    receipt = json.loads(card["receipt"]) if card["receipt"] else {}
                else:
                    receipt = await channel.deliver(node, json.loads(card["payload"]), json.loads(card["receipt"]) if card["receipt"] else None)
                store.done(card, key, receipt)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                delay = max(getattr(error, "retry", 0), min(2 ** min(card["failures"] + 1, 10), 900))
                store.failed(card, key, delay + random.uniform(0, 3))
                log.warning("Channel %s delivery postponed: %s", key, type(error).__name__)

    async def maintain():
        tick = 0
        while True:
            store.health(subscriptions, config.get("offline_seconds", 180))
            for event in wake.values():
                event.set()
            if tick % 120 == 0:
                store.cleanup()
            tick += 1
            await asyncio.sleep(30)

    async def lifecycle(app):
        tasks = []

        async def channel_worker(key, channel):
            while True:
                try:
                    start = getattr(channel, "start", None)
                    if start:
                        await start()
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    log.warning("Channel %s not ready: %s", key, type(error).__name__)
                    await asyncio.sleep(30)
            await worker(key, channel)

        try:
            tasks = [asyncio.create_task(channel_worker(key, channel)) for key, channel in channels.items()]
            tasks.append(asyncio.create_task(maintain()))
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for channel in channels.values():
                await channel.close()
            store.db.close()

    app.router.add_post("/v1/events", ingest)
    app.router.add_get("/healthz", health)
    app.cleanup_ctx.append(lifecycle)
    return app
