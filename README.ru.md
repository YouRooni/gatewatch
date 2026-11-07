# Gatewatch

<a href="README.md"><kbd>English</kbd></a>

[![Checks](https://github.com/YouRooni/gatewatch/actions/workflows/checks.yml/badge.svg)](https://github.com/YouRooni/gatewatch/actions/workflows/checks.yml) [![Release](https://img.shields.io/github/v/release/YouRooni/gatewatch)](https://github.com/YouRooni/gatewatch/releases/latest) [![License](https://img.shields.io/github/license/YouRooni/gatewatch)](LICENSE)

Gatewatch отправляет события инфраструктуры в Telegram. У каждого сервера своя тема, управление SSH-блокировками через Fail2ban подключается отдельно.

`Источники → очередь агента → HTTPS → хаб → Telegram`

Скрипты и сервисы могут отправлять события напрямую в хаб по HTTPS.

## Быстрый старт

Нужны Debian 12+ или Ubuntu 22.04+, Python 3.10+, бот и группа Telegram с темами. Выдай боту право управлять темами и подготовь свой числовой ID.

1. Установи хаб:

   ```sh
   git clone --branch v1.2.0 https://github.com/YouRooni/gatewatch.git
   cd gatewatch
   sudo bash scripts/install.sh hub
   ```

2. Настрой HTTPS перед хабом. Укажи базовый HTTPS-адрес при создании сервера через `gatewatch nodes add`.
3. Передай конфиг по SSH и выполни `sudo bash scripts/install.sh agent /path/to/agent.secret.json` на сервере. Для событий самого хаба тоже нужен агент.
4. Проверь связь в `/status`, отправь тестовое событие через `gatewatch emit` и проверь тему сервера.

[Установка и проверка (EN)](docs/operations.md) · [Обновление и откат (EN)](docs/releases.md) · [Безопасность (EN)](SECURITY.md)

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

## Разработка

[Правила и локальные проверки (EN)](CONTRIBUTING.md).

MIT · Author: [YouRooni.t.me](https://t.me/YouRooni)
