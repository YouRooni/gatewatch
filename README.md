# Gatewatch

<a href="README.ru.md"><kbd>Русский</kbd></a>

[![Checks](https://github.com/YouRooni/gatewatch/actions/workflows/checks.yml/badge.svg)](https://github.com/YouRooni/gatewatch/actions/workflows/checks.yml) [![Release](https://img.shields.io/github/v/release/YouRooni/gatewatch)](https://github.com/YouRooni/gatewatch/releases/latest) [![License](https://img.shields.io/github/license/YouRooni/gatewatch)](LICENSE)

Gatewatch sends infrastructure events to Telegram, with a topic per server and optional Fail2ban SSH controls.

`Sources → Agent outbox → HTTPS → Hub → Telegram`

Custom producers can also post directly to the hub over HTTPS.

## Quick start

Debian 12+ or Ubuntu 22.04+, Python 3.10+, a bot and a Telegram forum group. Give the bot **Manage Topics** and have your numeric Telegram user ID ready.

1. Install the hub:

   ```sh
   git clone --branch v1.2.0 https://github.com/YouRooni/gatewatch.git
   cd gatewatch
   sudo bash scripts/install.sh hub
   ```

2. Put the hub behind HTTPS. Use that base URL when creating a node with `gatewatch nodes add`.
3. Copy the generated config over SSH and run `sudo bash scripts/install.sh agent /path/to/agent.secret.json` on the node. Install an agent on the hub host too if you want its events.
4. Check the node in `/status`, then send a synthetic event with `gatewatch emit` and check its topic.

[Full setup and verification](docs/operations.md) · [Update and rollback](docs/releases.md) · [Security](SECURITY.md)

## Notifications

| Provider | Features | Setup |
| --- | --- | --- |
| Telegram | Topics per node, grouped alerts, inline controls | [Telegram](docs/channels/telegram.md) |

## Inputs

| Input | Events | Setup |
| --- | --- | --- |
| Journal | SSH and Fail2ban | [Journal](docs/sources/journal.md) |
| File | Fail2ban logs | [File](docs/sources/file.md) |
| CLI / HTTP | Events from scripts and services | [Custom events](docs/sources/custom.md) |

| Actions | Adapter |
| --- | --- |
| Ban / unban | [Fail2ban](docs/actions/fail2ban.md) |

Other channels, sources and actions can be added as [plugins](docs/extensions.md). Independent clients can use the [HTTP protocol](docs/protocol.md).

## Development

[Contributing and local checks](CONTRIBUTING.md).

MIT · Author: [YouRooni.t.me](https://t.me/YouRooni)
