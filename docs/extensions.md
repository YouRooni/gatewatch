# Extensions

Core flow: source → durable node outbox → HTTP v1 → hub store → channel delivery queue.

Source code is independent of notification channels. A channel receives a normalized card and a node record, not raw OS logs. Different channels keep their own delivery version, receipt and retry deadline. A slow/broken channel does not prevent another channel from delivering.

## Source plugin

Register an entry point in your own package:

```toml
[project.entry-points."gatewatch.sources"]
my-source = "my_package.source:MySource"
```

```python
from gatewatch.model import Event

class MySource:
    def __init__(self, config):
        self.config = config

    async def records(self, cursor):
        async for record in your_stream(after=cursor):
            event = Event(
                id=record.stable_id,
                kind="auth.failure",
                time=record.unix_time,
                ip=record.remote_ip,
                user=record.user,
                service="your-service"
            )
            yield record.cursor, [Event.parse(event.wire())]
```

`your_stream` is pseudocode. The async iterator yields `(cursor: str, events: list[Event])`. Each batch and cursor are committed atomically by the agent before the next batch. IDs must be stable across rereads. Bound batches to 64 events; wait for I/O instead of busy loops. Raise on transient errors; the runner restarts the source after a delay using the committed cursor. Cancellation must close subprocesses, files and connections.

Configure and install the plugin into the same venv as Gatewatch:

```json
{"sources": {"my-stable-source-id": {"type": "my-source", "option": "value"}}}
```

For Android or another runtime, implement [HTTP protocol v1](protocol.md) directly. A separate implementation only needs durable IDs, queueing, heartbeat and HTTPS. It does not need to import Gatewatch or use Python.

## Channel plugin

```toml
[project.entry-points."gatewatch.channels"]
my-channel = "my_package.channel:MyChannel"
```

```python
class MyChannel:
    def __init__(self, config, store, channel_id):
        self.config = config
        self.store = store
        self.channel_id = channel_id

    async def start(self):
        pass

    async def deliver(self, node, card, receipt):
        response = await your_api.send_or_update(card, receipt)
        return {"message_id": response.id}

    async def close(self):
        pass
```

`your_api` is pseudocode. `start` is optional. `deliver` returns a JSON-serializable receipt. The next update to the same card receives that receipt. For an API without edits, choose whether to send a summary or a new notification. Persist only small receipts. Do not log tokens or private payloads.

Raising an exception leaves the delivery pending. Optionally attach numeric `retry` seconds to the exception for provider rate limits. Network timeouts, retry/backoff and output escaping are the channel's responsibility. `close` must stop owned tasks and close clients. The hub runs one worker per channel, so calls to that channel's `deliver` are sequential. Keep polling/background tasks isolated.

`node` contains `id`, `name`, `seen`, `health`, `muted` and `enabled`; credential digests are excluded. `card` contains a normalized event plus `count`, `first`, `last` and `users` for authentication groups. Internal health cards have kinds `node.online` / `node.offline` and omit IP/user fields.

Use `store.state(key, default)` and `store.set_state(key, value)` for small JSON state, with keys prefixed by your channel ID. Treat node names and user fields as untrusted strings. Keep channel IDs stable in config to preserve receipts.

Plugin APIs are version 0.1 and may evolve before 1.0. Pin a compatible Gatewatch release in external plugin packages. Plugins run in-process with the service's permissions: install only trusted packages.
