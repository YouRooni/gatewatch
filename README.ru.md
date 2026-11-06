# Gatewatch

<a href="README.md"><kbd>English</kbd></a>

Собирает события инфраструктуры и доставляет их через подключаемые каналы уведомлений. Очереди на диске, отдельные ключи нод и опциональные действия.

## Уведомления

| Провайдер | Возможности | Настройка |
| --- | --- | --- |
| Telegram | Тема на ноду, группировка событий, inline-кнопки | [Telegram](docs/channels/telegram.md) |

## Источники

| Источник | События | Настройка |
| --- | --- | --- |
| Journal | SSH и Fail2ban | [Journal](docs/sources/journal.md) |
| Файл | Логи Fail2ban | [File](docs/sources/file.md) |
| CLI / HTTP | События скриптов и сервисов | [Свои события](docs/sources/custom.md) |

| Действия | Адаптер |
| --- | --- |
| Бан / разбан | [Fail2ban](docs/actions/fail2ban.md) |

Другие каналы, источники и действия подключаются как [плагины](docs/extensions.md). Свои клиенты могут использовать [HTTP-протокол](docs/protocol.md).

## Установка

Debian 12+ или Ubuntu 22.04+, Python 3.10+.

```sh
git clone --branch v1.1.1 https://github.com/YouRooni/gatewatch.git
cd gatewatch
sudo bash scripts/install.sh hub
```

[Подключение нод](docs/operations.md) · [Обновление / откат](docs/releases.md) · [Безопасность](SECURITY.md)

## Разработка

```sh
python -m pip install -e .
python -m unittest discover -s tests -q
```

MIT · Author: [YouRooni.t.me](https://t.me/YouRooni)
