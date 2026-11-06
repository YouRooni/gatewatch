# Setup

## Hub

Run `sudo bash scripts/install.sh hub` from the repository. The installer creates an unprivileged service, configures the default notification channel, and writes `/etc/gatewatch/hub.secret.json`.

Configure a notification provider: [Telegram](channels/telegram.md). Provider permissions and commands are documented separately.

The hub listens on `127.0.0.1:8787`. Put it behind HTTPS. Add these locations to an existing TLS Nginx server, or use [Caddy](../deploy/Caddyfile.example):

```nginx
location = /gatewatch/v1/events {
    client_max_body_size 128k;
    proxy_pass http://127.0.0.1:8787/v1/events;
}
location = /gatewatch/v1/commands {
    proxy_pass http://127.0.0.1:8787/v1/commands;
    proxy_read_timeout 35s;
}
```

Check `nginx -t` before reloading. Never log Authorization headers.

## Nodes

On the hub:

```sh
sudo -u gatewatch /opt/gatewatch/bin/gatewatch nodes add 'Node-1' \
  --address 203.0.113.10 --endpoint https://example.com/gatewatch \
  --out /var/lib/gatewatch/node-1.secret.json
```

Transfer that file to the node over SSH. From a copy of this repository on the node:

```sh
sudo bash scripts/install.sh agent /path/to/node-1.secret.json
```

Delete transfer copies after verification. The first heartbeat creates a topic. Renaming it is safe; a deleted topic is recreated on the next delivery.

Manage identities with `nodes list`, `nodes rename UUID NAME`, `nodes address UUID IP`, `nodes revoke UUID`, `nodes remove UUID`, or `nodes rotate UUID --endpoint URL --out FILE`. Run these as `gatewatch` on the hub. Renaming an identity preserves its existing topic name.

## Optional integrations

[Journal](sources/journal.md) · [File](sources/file.md) · [Custom events](sources/custom.md) · [Fail2ban actions](actions/fail2ban.md) · [Password capture](integrations/pam.md)

## Configuration

Restart the service after editing its config. Validate with `gatewatch --config PATH check`.

| Hub option | Default |
| --- | --- |
| `aggregation_seconds` | `300` |
| `offline_seconds` | `180` |
| `queue_limit` | `100000` deliveries |
| `event_limit` | `1000000` deduplication records |

Agent defaults: a journald source, 30-second heartbeat, up to 50,000 queued events for seven days. Full queues apply backpressure; recovery still depends on source log retention. Hub data is retained for 14 days. Failed-password samples are limited to eight distinct values per card.

## Maintenance

Logs: `journalctl -u gatewatch-hub -f` or `journalctl -u gatewatch-agent -f`.

Back up with `sudo -u gatewatch /opt/gatewatch/bin/gatewatch backup /var/lib/gatewatch/snapshot.db`; store the secret config separately. Use the [versioned update manager](releases.md) for upgrades and rollback. `gatewatch doctor` checks local database and plugin readiness without sending messages.

Remove services with `sudo /opt/gatewatch/bin/gatewatch-manage uninstall all`. Configs and data are retained; removal never resets firewall rules.

For a synthetic node, create a separate identity and database, use `"sources":{"demo":{"type":"demo"}}` and optionally `"actions":{"type":"demo"}`. Demo actions do not modify the firewall.
