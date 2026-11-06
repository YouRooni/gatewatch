# Setup

## Hub

Run `sudo bash scripts/install.sh hub` from the repository. The installer creates an unprivileged service, asks for Telegram credentials, and writes `/etc/gatewatch/hub.secret.json`.

Use a forum group. The bot needs **Manage Topics** permission; `owner_ids` controls who may use commands and buttons.

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

Fail2ban must already be running. To read its events, set `logtarget = SYSLOG` in `/etc/fail2ban/fail2ban.d/gatewatch.local` and apply `fail2ban-client set logtarget SYSLOG`. Alternatively, use a readable file source: `{"type":"file","path":"/var/log/fail2ban.log"}`. Do not enable both for the same log.

On a node:

```sh
sudo bash scripts/install-actions.sh enable
sudo bash scripts/install-pam.sh enable
```

Use `disable` to undo either integration. Actions are restricted to ban/unban in the existing `sshd` jail and use its ban duration. The root-owned policy `/etc/gatewatch/actions-policy.json` protects server/admin addresses. Update it when your admin IP changes. No root helper remains resident while idle.

PAM capture supports standard Debian/Ubuntu `pam_unix` stacks; custom authentication needs review. It changes only the SSH PAM stack and backs it up first. Keep an SSH session open while checking a new login. Captured failed passwords reach the database and Telegram; successful passwords are discarded. OpenSSH substitutes passwords for invalid users, so those originals are unavailable. See [security](../SECURITY.md).

## Configuration

Restart the service after editing its config. Validate with `gatewatch --config PATH check`.

| Hub option | Default |
| --- | --- |
| `aggregation_seconds` | `300` |
| `offline_seconds` | `180` |
| `queue_limit` | `100000` deliveries |
| `event_limit` | `1000000` deduplication records |

Telegram channel options: `language` (`ru`/`en`), `timezone`, `custom_emoji`, `events`. Defaults notify failures, bans, unbans and agent connectivity. Add `auth.success` to an explicit `events` list to enable successful sign-ins.

Agent defaults: a journald source, 30-second heartbeat, up to 50,000 queued events for seven days. Full queues apply backpressure; recovery still depends on source log retention. Hub data is retained for 14 days. Failed-password samples are limited to eight distinct values per card.

For offline geolocation, install `gatewatch[geo]`, run `gatewatch geo-update` as the service user, and set the channel's `geo` object:

```json
{"city_database":"/var/lib/gatewatch/geo/dbip-city.mmdb","asn_database":"/var/lib/gatewatch/geo/dbip-asn.mmdb","attribution":"dbip"}
```

DB-IP Lite requires attribution. Update monthly and restart the hub. No lookup requests leave your server.

## Maintenance

Logs: `journalctl -u gatewatch-hub -f` or `journalctl -u gatewatch-agent -f`.

Back up with `sudo -u gatewatch /opt/gatewatch/bin/gatewatch backup /var/lib/gatewatch/snapshot.db`; store the secret config separately. Use the [versioned update manager](releases.md) for upgrades and rollback. `gatewatch doctor` checks local database and plugin readiness without sending messages.

Remove services with `sudo /opt/gatewatch/bin/gatewatch-manage uninstall all`. Configs and data are retained; removal never resets firewall rules.

For a synthetic node, create a separate identity and database, use `"sources":{"demo":{"type":"demo"}}` and optionally `"actions":{"type":"demo"}`. Demo actions do not modify the firewall.
