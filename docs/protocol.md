# Event protocol v1

Any HTTPS client can act as a node. Linux, journald and Python are not part of the wire protocol.

## Request

`POST /v1/events`, `Content-Type: application/json`, `Authorization: Bearer NODE_TOKEN`.

```json
{
  "version": 1,
  "node_id": "c07876c1-9694-4b65-9b65-f89bc233a103",
  "events": [{
    "id": "8fd69ecb-d086-4b67-9074-91611754ab1f",
    "kind": "auth.failure",
    "time": 1793977200,
    "ip": "203.0.113.42",
    "user": "root",
    "method": "password",
    "port": 54321,
    "service": "ssh",
    "detail": ""
  }]
}
```

The timestamp above is illustrative; clients must send current Unix seconds in UTC. Allowed age: seven days, future tolerance: five minutes. Synchronize node clocks.

| Field | Constraint |
| --- | --- |
| `id` | Required; stable ASCII `[a-zA-Z0-9_-]`, 16–80 characters |
| `kind` | `auth.failure`, `auth.success`, `access.ban`, `access.unban`, `event` |
| `time` | Required; finite numeric Unix timestamp |
| `ip` | IPv4 or IPv6; optional for `event` |
| `user` | Optional; printable string, at most 128 characters |
| `method` | Optional; printable string, at most 32 characters |
| `port` | Optional; integer 0–65535; remote/client port, zero if unknown |
| `service` | Optional; printable string, at most 64 characters; default `ssh` |
| `detail` | Optional printable text; 1024 characters for `event`, otherwise 128 |
| `title` | Required for `event` only; nonempty printable text, at most 128 characters |
| `level` | Required for `event` only; `info`, `warning`, `error` |
| `password` | Optional sensitive string, at most 512 characters; allowed only for `auth.failure` with method `password` |

Never include successful passwords, tokens or raw log messages. The optional `password` field is specifically for intentionally configured failed-password capture; omit it when unavailable. Unknown event fields are rejected. `event` requires hub 1.1+; upgrade the hub first. Authentication-only fields and passwords are rejected on service events. Use a meaningful `service` such as `backup` or `vpn`; include a stable event ID and current timestamp. Other runtimes can implement this protocol without Gatewatch/Python.

Maximum batch: 100 events. Maximum request body: 128 KiB. An empty batch is a heartbeat. Send it every 30 seconds. A valid request refreshes the last-seen time even if all events are duplicates.

Optional `agent_info` reports `{"version":"1.0.0","sources":{"journal":"ok"}}`; source states are `starting`, `ok` or `error`, up to 16 sources. The hub derives connectivity and source-health notifications from these reports. Such requests receive `protocol` and `hub_version` in the acknowledgement. Legacy clients may omit the field.

An agent with actions can include `agent_info.action_adapter`. Telegram IP controls support `fail2ban` and `demo`. The action payload and result fields remain protocol 1.

An otherwise valid batch containing expired events returns HTTP 400 with `{"error":"events_expired","ids":[...]}`. Agents may remove those IDs from the submitted batch and retry the rest. A generic HTTP 400 does not identify disposable events. An authenticated read-only `GET /v1/probe?node_id=...` checks the identity without recording a heartbeat.

## Acknowledgement

Optional management: nodes advertise `"capabilities": ["ban", "unban"]` in the envelope only with an enabled adapter. `GET /v1/commands?node_id=UUID` with the same Bearer credential waits up to 25 seconds and returns `{"commands": [{"id": "32-hex", "action": "ban", "ip": "203.0.113.42", "expires": 1793977380}]}`. The hub leases commands for 60 seconds. Check expiry before execution and durably cache outcomes by ID.

Include results in a normal event/heartbeat request as `"results": [{"id": "32-hex", "ok": true, "detail": "IP blocked"}]`, at most 20 items. The response acknowledges IDs in `results_accepted`; `results_rejected` lists results with no matching retained command. Both lists can be removed from the local retry queue. Results are scoped to the authenticated node. Do not expose arbitrary remote shell capabilities.

`200 {"accepted": N}` means the complete batch has been committed to durable storage or recognized as already accepted. It does not mean the notification has reached Telegram. The hub validates the complete batch before committing any event.

- `400`: invalid schema/version/time; repair the batch, do not acknowledge locally.
- `401`: invalid/revoked node key; fix credentials.
- `413`: request body too large; use a smaller batch.
- `503`: hub capacity reached; retain locally and back off.
- Network error / other non-200: retain and retry with backoff and jitter.

Deduplication key is `(node_id, event.id)`. Reuse the same ID when retrying. Store events locally before advancing the source cursor. Never follow redirects while sending credentials. Require a trusted HTTPS certificate; loopback HTTP is allowed for local agents/tunnels only.

Acceptance is idempotent within the 14-day retention period. Telegram delivery is at least once: ambiguous network failures may produce duplicate messages. One node key cannot submit events on behalf of another node.
