import asyncio
import contextlib
import json
import logging
import random
import time

from aiohttp import web

from . import PROTOCOL_VERSION, __version__
from .config import DEFAULT_EVENTS
from .model import Event
from .plugins import plugin
from .storage import HubStore

log = logging.getLogger(__name__)


def create_app(config, store=None):
    store = store or HubStore(
        config["database"],
        config.get("aggregation_seconds", 300),
        config.get("queue_limit", 100000),
        config.get("event_limit", 1000000),
    )
    channels = {
        key: plugin("channels", options["type"])(options, store, key)
        for key, options in config["channels"].items()
    }
    subscriptions = {
        key: set(options.get("events", DEFAULT_EVENTS)) for key, options in config["channels"].items()
    }
    wake = {key: asyncio.Event() for key in channels}
    app = web.Application(client_max_size=128 * 1024)

    async def ingest(request):
        authorization = request.headers.get("Authorization", "")
        if not authorization.startswith("Bearer ") or len(authorization) > 300:
            raise web.HTTPUnauthorized()
        try:
            data = await request.json()
            if (
                not isinstance(data, dict)
                or type(data.get("version")) is not int
                or data["version"] != 1
                or not isinstance(data.get("node_id"), str)
            ):
                raise ValueError("Invalid envelope")
            if not store.authenticate(data["node_id"], authorization[7:]):
                raise web.HTTPUnauthorized()
            batch = data.get("events")
            if not isinstance(batch, list) or len(batch) > 100:
                raise ValueError("Batch must contain at most 100 events")
            events = [Event.parse(event) for event in batch]
            capabilities = data.get("capabilities", [])
            results = data.get("results", [])
            agent_info = data.get("agent_info")
            if agent_info is not None:
                if (
                    not isinstance(agent_info, dict)
                    or not isinstance(agent_info.get("version"), str)
                    or len(agent_info["version"]) > 64
                ):
                    raise ValueError("Invalid agent info")
                sources = agent_info.get("sources", {})
                if (
                    not isinstance(sources, dict)
                    or len(sources) > 16
                    or any(
                        not isinstance(key, str) or len(key) > 64 or status not in ("ok", "error", "starting")
                        for key, status in sources.items()
                    )
                ):
                    raise ValueError("Invalid source status")
            if (
                not isinstance(capabilities, list)
                or len(capabilities) > 16
                or any(capability not in ("ban", "unban") for capability in capabilities)
            ):
                raise ValueError("Invalid capabilities")
            store.control.validate_results(results)
            store.ingest(data["node_id"], events, subscriptions)
            accepted_results = store.control.accept(data["node_id"], capabilities, results)
            if agent_info is not None:
                store.set_state("agent:" + data["node_id"], agent_info)
            else:
                store.set_state("agent:" + data["node_id"], {"version": "legacy", "sources": {}})
        except (ValueError, TypeError, KeyError):
            raise web.HTTPBadRequest(text="Invalid event batch") from None
        except OverflowError:
            raise web.HTTPServiceUnavailable(text="Queue full", headers={"Retry-After": "30"}) from None
        for event in wake.values():
            event.set()
        response = {"accepted": len(events)}
        if results:
            response["results_accepted"] = accepted_results
            response["results_rejected"] = [
                result["id"] for result in results if result["id"] not in accepted_results
            ]
        if agent_info is not None:
            response.update(protocol=PROTOCOL_VERSION, hub_version=__version__)
        return web.json_response(response)

    async def commands(request):
        node_id = request.query.get("node_id", "")
        authorization = request.headers.get("Authorization", "")
        if (
            not authorization.startswith("Bearer ")
            or len(authorization) > 300
            or not store.authenticate(node_id, authorization[7:])
        ):
            raise web.HTTPUnauthorized()
        signal = store.control.signal(node_id)
        signal.clear()
        jobs = store.control.fetch(node_id)
        if not jobs:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(signal.wait(), 25)
            if not store.authenticate(node_id, authorization[7:]):
                raise web.HTTPUnauthorized()
            jobs = store.control.fetch(node_id)
        return web.json_response({"commands": jobs})

    async def health(request):
        return web.json_response(
            {"status": "ok", "protocol": PROTOCOL_VERSION, "version": __version__, "schema": 1}
        )

    async def worker(key, channel):
        while True:
            wake[key].clear()
            job = store.control.due(key)
            if job:
                try:
                    notify = getattr(channel, "action_result", None)
                    if notify:
                        await notify(dict(job))
                    store.control.done(job)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    store.control.failed(job)
                    log.warning("Action status postponed: %s", type(error).__name__)
            card = store.due(key)
            if not card:
                try:
                    deadline = store.next_due(key)
                    await asyncio.wait_for(
                        wake[key].wait(), min(15, max(0.05, deadline - time.time())) if deadline else 15
                    )
                except asyncio.TimeoutError:
                    pass
                continue
            node = dict(store.db.execute("SELECT * FROM nodes WHERE id=?", (card["node"],)).fetchone())
            node.pop("token", None)
            try:
                if node["muted"] > time.time() and card["kind"] == "auth.failure":
                    receipt = json.loads(card["receipt"]) if card["receipt"] else {}
                else:
                    receipt = await channel.deliver(
                        node,
                        json.loads(card["payload"]),
                        json.loads(card["receipt"]) if card["receipt"] else None,
                    )
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
            store.control.cleanup()
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
    app.router.add_get("/v1/commands", commands)
    app.router.add_get("/healthz", health)

    async def shutdown(app):
        for signal in store.control.signals.values():
            signal.set()

    app.on_shutdown.append(shutdown)
    app.cleanup_ctx.append(lifecycle)
    return app
