# Extensions

Flow: source → durable outbox → HTTP → hub → channel. Action adapters are optional and independent of log collection.

Register plugins in your package:

```toml
[project.entry-points."gatewatch.sources"]
my-source = "my_package:MySource"

[project.entry-points."gatewatch.channels"]
my-channel = "my_package:MyChannel"

[project.entry-points."gatewatch.actions"]
my-actions = "my_package:MyActions"
```

| Plugin | Contract |
| --- | --- |
| Source | `__init__(config)`; async `records(cursor)` yields `(str_cursor, list[Event])` |
| Channel | `__init__(config, store, channel_id)`; async `deliver(node, card, receipt)` returns a JSON receipt; async `close()` |
| Actions | `__init__(config)`; `capabilities`; async `execute(job)` returns `{"ok": bool, "detail": str}` |

Select the entry-point name with `type` in the corresponding config object. Install the package in Gatewatch's venv.

## Sources

Use stable event IDs across rereads. The agent commits events and cursor together. Bound batches to 64 events, wait for I/O, and close resources on cancellation. Errors restart the source from its committed cursor. Construct events with `gatewatch.model.Event` and validate them with `Event.parse(event.wire())`.

Other runtimes, including Android, can implement the [HTTP protocol](protocol.md) without importing Gatewatch.

## Channels

A card contains normalized event fields plus `count`, `first`, `last`, and `users`. Service cards use `event` with title, level and detail. Health cards use `node.online`/`node.offline`/`node.degraded`/`node.recovered` and omit IP/user fields. Captured failed passwords may appear as `password` and a bounded `passwords` sample list.

Receipts are channel-specific. Return a small receipt so later card updates can edit the same notification. Raise on failure; an exception may expose numeric `retry` seconds. Optional async hooks: `start()` and `action_result(job)`. Each channel has a sequential delivery worker.

Use `store.state(key, default)` and `store.set_state(key, value)` for small JSON state; prefix keys with your channel ID. Keep channel IDs stable in config. Escape user-controlled fields and never log credentials.

## Actions

Jobs contain `id`, `action`, `ip`, `expires`. Core supports `ban` and `unban`; new action names require a protocol extension. Result detail must be a non-sensitive printable string of at most 160 characters.

Do not build shell commands from job fields. Keep privileges in a restricted helper, as the built-in Fail2ban adapter does. The `demo` adapter performs no firewall operations.

Pin compatible Gatewatch/plugin versions; plugins run with the host service's permissions.
