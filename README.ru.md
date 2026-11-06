# Gatewatch

<a href="README.md"><kbd>English</kbd></a>

Уведомления SSH и Fail2ban в Telegram, с отдельной темой для каждого сервера. Очередь на диске, опциональные кнопки блокировки, расширяемые источники и каналы.

## Установка

Debian 12+ или Ubuntu 22.04+, Python 3.10+.

```sh
git clone https://github.com/YouRooni/gatewatch.git
cd gatewatch
sudo bash scripts/install.sh hub
```

Мастер запросит токен бота, ID группы и владельца. Боту нужны права на управление темами.

[Подключение нод и настройка HTTPS →](docs/operations.md)

## Управление

`/status` · `/node` · `/mute 1h` · `/unmute`

Кнопки бана/разбана требуют отдельного адаптера Fail2ban. Уведомления об успешных входах и захват паролей по умолчанию выключены.

## Разработка

```sh
python -m pip install -e .
python -m unittest discover -s tests -q
```

[Настройка](docs/operations.md) · [Протокол](docs/protocol.md) · [Расширения](docs/extensions.md) · [Безопасность](SECURITY.md)

MIT · Author: [YouRooni.t.me](https://t.me/YouRooni)
