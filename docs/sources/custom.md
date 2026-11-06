# Custom events

Requires hub 1.1+. Report backups, VPN checks, jobs or application alerts without writing a parser.

## Local scripts

Use an installed agent's config and permissions:

```sh
sudo -u gatewatch /opt/gatewatch/bin/gatewatch \
  --config /etc/gatewatch/agent.secret.json \
  emit 'Backup failed' --service backup --level error --detail 'Storage unavailable'
```

The event enters the persistent local outbox; the running agent delivers it. The command reports an event ID. Optional `--id` keeps retries idempotent; `--ip` adds a relevant address. Levels: `info`, `warning`, `error`. No IP is required.

## Other clients

Send `kind: "event"`, `title`, `service`, `level`, `detail`, plus a stable `id` and current `time`, to the authenticated [HTTP endpoint](../protocol.md). Linux and Python are not required by the protocol; Android or another runtime can implement the same client.

The core transports explicit events. It does not automatically discover application failures or scrape every log. Use a [source plugin](../extensions.md) for continuous collection. Never include credentials or raw dumps in event text.
