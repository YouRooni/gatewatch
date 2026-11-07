# Telegram

One forum topic per node, grouped failed sign-ins and optional inline ban controls. Topic bindings use IDs; renaming is safe, deleted topics are recreated.

## Setup

Create a bot with BotFather. Add it as an administrator to a group with topics and grant **Manage Topics**. The hub setup wizard asks for its token, the negative group ID and your numeric user ID. `owner_ids` restricts commands and buttons.

Channel config in `/etc/gatewatch/hub.secret.json`:

```json
{"channels":{"telegram":{"type":"telegram","token":"BOT_TOKEN","chat_id":-1001234567890,"owner_ids":[123456789],"language":"en","timezone":"UTC"}}}
```

Keep channel IDs stable. They identify delivery receipts and topic bindings.

## Controls

`/status` · `/node` · `/mute 1h` · `/unmute`

`/status` lists servers and links to their topics. `/node` opens a reusable panel with connection state, delivery backlog and pause controls. A pause suppresses failed sign-in notifications for that server across all channels until the displayed time. Collection, health alerts and bans continue. Suppressed notifications are skipped on resume.

**Manage IP** shows the last observation and asks for confirmation before changing SSH access. A successful command describes the `sshd` jail only. A timeout after dispatch leaves an unconfirmed result until the agent responds or a newer observation resolves it. The [Fail2ban adapter](../actions/fail2ban.md) enables actions. Successful sign-ins and [password capture](../integrations/pam.md) are off by default.

| Option | Default |
| --- | --- |
| `language` | `ru`; also `en` |
| `timezone` | `UTC` |
| `custom_emoji` | `true` |
| `rich_messages` | `true`; standard HTML fallback |
| `show_node_name` | `false` |
| `time_display` | `auto`; also `always`, `never` |
| `events` | Service events, failures, bans, unbans, node/source health |

An explicit `events` list replaces defaults. Add `auth.success` to enable successful sign-ins, or `event` to receive custom events. Link previews are disabled.

## Location

Optional offline GeoIP: install `gatewatch[geo]`, run `gatewatch geo-update` as the service user, and add a `geo` object to this channel:

```json
{"city_database":"/var/lib/gatewatch/geo/dbip-city.mmdb","asn_database":"/var/lib/gatewatch/geo/dbip-asn.mmdb","attribution":"dbip"}
```

DB-IP Lite requires attribution. Update monthly and restart the hub. Lookups stay local.
