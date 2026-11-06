# Gatewatch

<a href="README.ru.md"><kbd>Русский</kbd></a>

Collect infrastructure events and deliver them through pluggable notification channels. Persistent queues, per-node identities, and optional remote actions.

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

## Install

Debian 12+ or Ubuntu 22.04+, Python 3.10+.

```sh
git clone --branch v1.1.0 https://github.com/YouRooni/gatewatch.git
cd gatewatch
sudo bash scripts/install.sh hub
```

[Connect nodes](docs/operations.md) · [Update / rollback](docs/releases.md) · [Security](SECURITY.md)

## Development

```sh
python -m pip install -e .
python -m unittest discover -s tests -q
```

MIT · Author: [YouRooni.t.me](https://t.me/YouRooni)
