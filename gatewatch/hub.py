import asyncio
import contextlib
import json
import logging
import random
import time

from aiohttp import web

from . import PROTOCOL_VERSION, __version__
from .config import DEFAULT_EVENTS
from .model import EVENT_MAX_AGE, Event
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
    store.offline_seconds = config.get("offline_seconds", 180)
    channels = {
        key: plugin("channels", options["type"])(options, store, key)
        for key, options in config["channels"].items()
    }
    subscriptions = {
        key: set(options.get("events", DEFAULT_EVENTS)) for key, options in config["channels"].items()
    }
    wake = {key: asyncio.Event() for key in channels}
    channel_status = dict.fromkeys(channels, "starting")
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
            now = time.time()
            events = [Event.parse(event, allow_expired=True, now=now) for event in batch]
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
                adapter = agent_info.get("action_adapter", "")
                if (
                    not isinstance(sources, dict)
                    or len(sources) > 16
                    or not isinstance(adapter, str)
                    or len(adapter) > 64
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
            expired = [event.id for event in events if event.time < now - EVENT_MAX_AGE]
            if expired:
                return web.json_response({"error": "events_expired", "ids": expired}, status=400)
            store.ingest(data["node_id"], events, subscriptions)
            if agent_info is not None:
                store.set_state("agent:" + data["node_id"], agent_info)
            else:
                store.set_state("agent:" + data["node_id"], {"version": "legacy", "sources": {}})
            accepted_results = store.control.accept(data["node_id"], capabilities, results)
        except (ValueError, TypeError, KeyError):
            raise web.HTTPBadRequest(text="Invalid event batch") from None
        except OverflowError:
            return web.json_response({"error": "queue_full"}, status=503, headers={"Retry-After": "30"})
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

    async def readiness(request):
        pending = store.pending_count()
        ready = pending < store.max_pending and all(
            channel_status[key] == "ready" and getattr(channel, "poll_healthy", True)
            for key, channel in channels.items()
        )
        return web.json_response({"status": "ready" if ready else "degraded"}, status=200 if ready else 503)

    async def probe(request):
        authorization = request.headers.get("Authorization", "")
        if (
            not authorization.startswith("Bearer ")
            or len(authorization) > 300
            or not store.authenticate(request.query.get("node_id", ""), authorization[7:])
        ):
            raise web.HTTPUnauthorized()
        return web.json_response({"protocol": PROTOCOL_VERSION})

    def delivery_error(error):
        code = getattr(error, "code", None)
        if code == 401:
            return "credentials"
        if code == 403:
            return "permissions"
        if code == 429:
            return "rate_limited"
        if code == 400:
            return "request_rejected"
        return "unavailable"

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
                    store.control.failed(job, max(30, getattr(error, "retry", 0)))
                    channel_status[key] = delivery_error(error)
                    log.warning("Channel %s action status postponed: %s", key, channel_status[key])
                if not store.due(key) and store.control.due(key):
                    continue
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
                    channel_status[key] = "ready"
                store.done(card, key, receipt)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                delay = max(getattr(error, "retry", 0), min(2 ** min(card["failures"] + 1, 10), 900))
                store.failed(card, key, delay + random.uniform(0, 3))
                channel_status[key] = delivery_error(error)
                log.warning("Channel %s delivery postponed: %s", key, channel_status[key])

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
                    channel_status[key] = "ready"
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    channel_status[key] = (
                        "permissions" if isinstance(error, PermissionError) else delivery_error(error)
                    )
                    log.warning("Channel %s not ready: %s", key, channel_status[key])
                    await asyncio.sleep(max(30, getattr(error, "retry", 0)))
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
    app.router.add_get("/readyz", readiness)
    app.router.add_get("/v1/probe", probe)

    async def shutdown(app):
        for signal in store.control.signals.values():
            signal.set()

    app.on_shutdown.append(shutdown)
    app.cleanup_ctx.append(lifecycle)
    return app
