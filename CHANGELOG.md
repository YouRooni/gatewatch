# Changelog

## 1.2.0

- Scope access observations to the node, adapter, jail and IP.
- Keep command state and result subscribers across restarts.
- Add IP confirmation, a server panel and paginated Telegram status.
- Expire stale outbox entries independently of fresh events.
- Add hub readiness and authenticated node probes.
- Allow retrying a partial install with its existing config.

## 1.1.1

- Fix installation with a restrictive umask.

## 1.1.0

- Custom service events through the CLI and HTTP API.
- Provider, source and action documentation split into separate guides.
- Infrastructure-focused project description and integration tables.

## 1.0.0

- SSH and Fail2ban events with one Telegram topic per node.
- Persistent queues, grouped attempts and optional ban controls.
- Optional PAM capture and offline GeoIP.
- Versioned installs, database migrations, backups and rollback.
- Source health checks and extensible source, action and channel adapters.
