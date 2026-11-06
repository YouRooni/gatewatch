# Gatewatch

<a href="README.ru.md"><kbd>Русский</kbd></a>

SSH and Fail2ban notifications in Telegram, with a topic per server. Includes persistent queues, optional ban controls, and pluggable sources and channels.

## Install

Debian 12+ or Ubuntu 22.04+, Python 3.10+.

```sh
git clone https://github.com/YouRooni/gatewatch.git
cd gatewatch
sudo bash scripts/install.sh hub
```

The wizard asks for your bot token, group ID and owner ID. Give the bot permission to manage topics.

[Connect nodes and configure HTTPS →](docs/operations.md)

## Controls

`/status` · `/node` · `/mute 1h` · `/unmute`

Ban/unban buttons require the optional Fail2ban adapter. Successful sign-ins and password capture are off by default.

## Development

```sh
python -m pip install -e .
python -m unittest discover -s tests -q
```

[Configuration](docs/operations.md) · [Protocol](docs/protocol.md) · [Extensions](docs/extensions.md) · [Security](SECURITY.md)

MIT · Author: [YouRooni.t.me](https://t.me/YouRooni)
