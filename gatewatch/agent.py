import asyncio
import logging
import random
import re
import time
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
    base = validate_endpoint(config["endpoint"])
    endpoint = base + "/v1/events"
    store = AgentStore(config["database"], config.get("queue_limit", 50000))
    wake = asyncio.Event()
    actions = plugin('actions',config['actions']['type'])(config['actions']) if config.get('actions') else None

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
                results = store.action_results()
                try:
                    async with session.post(endpoint, json={"version": 1, "node_id": config["node_id"], "events": batch,
                                                           'capabilities':actions.capabilities if actions else [],'results':results},
                                            headers={"Authorization": "Bearer " + config["token"]}, allow_redirects=False) as response:
                        if response.status != 200:
                            raise RuntimeError(f"HTTP {response.status}")
                        result = await response.json()
                        if result.get("accepted") != len(batch):
                            raise RuntimeError("Invalid acknowledgement")
                    store.ack(batch)
                    store.ack_actions(result.get('results_accepted',[]))
                    delay = 1
                    if store.has_pending() or (store.action_results() and result.get('results_accepted')):
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

    async def receive_commands():
        delay = 1
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=35)) as session:
            while True:
                try:
                    async with session.get(base+'/v1/commands',params={'node_id':config['node_id']},headers={'Authorization':'Bearer '+config['token']},allow_redirects=False) as response:
                        if response.status != 200:
                            raise RuntimeError(f'HTTP {response.status}')
                        jobs = (await response.json())['commands']
                    if not isinstance(jobs,list) or len(jobs)>5:
                        raise ValueError('Invalid commands')
                    for job in jobs:
                        if not isinstance(job,dict) or not re.fullmatch('[a-f0-9]{32}',job.get('id','')):
                            raise ValueError('Invalid command id')
                        cached = store.action_result(job['id'])
                        if cached:
                            outcome = cached
                        elif job.get('expires',0) <= time.time():
                            outcome = {'ok':False,'detail':'Command expired'}
                        else:
                            try:
                                outcome = await asyncio.wait_for(actions.execute(job),15)
                            except Exception:
                                outcome = {'ok':False,'detail':'Action failed'}
                        store.save_action(job['id'],outcome)
                        wake.set()
                    delay = 1
                except asyncio.CancelledError:
                    raise
                except (aiohttp.ClientError,asyncio.TimeoutError,RuntimeError,ValueError,KeyError) as error:
                    log.warning('Commands postponed: %s',type(error).__name__)
                    await asyncio.sleep(delay+random.uniform(0,delay/4))
                    delay = min(delay*2,60)

    tasks = [asyncio.create_task(collect(key, value)) for key, value in config.get("sources", {"journal": {"type": "journal"}}).items()]
    tasks.append(asyncio.create_task(send()))
    if actions:
        tasks.append(asyncio.create_task(receive_commands()))
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        store.db.close()
