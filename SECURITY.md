# Security

Gatewatch reports security events. It does not replace SSH hardening, Fail2ban, firewall rules or an external availability monitor.

- Nodes use individual random 256-bit credentials. The hub stores SHA-256 digests, not recoverable node keys.
- A compromised node can forge its own events or flood its own identity until revoked. It cannot authenticate as another node without that node's key.
- The Telegram bot token stays on the hub. Bot commands require both the configured group ID and an explicit owner user ID. Anonymous admin messages are not authorized.
- Remote ingress requires HTTPS with certificate verification. The default listener is loopback-only. Deploy a TLS reverse proxy; do not put credentials in URLs or proxy logs.
- Batch size, body size, queues and deduplication records are bounded. An authenticated flood can exhaust shared capacity; use ingress rate limits and isolate hostile tenants in separate hubs.
- Account names and node names are HTML-escaped. Sources discard raw log messages; attempted passwords are not collected.
- SQLite and configs contain personal/network metadata. Protect backups and local files. Linux installer uses an unprivileged system account and systemd filesystem restrictions.
- The same service user can read node configs on a host running both roles. For stricter tenant isolation, use separate hosts/accounts.
- Windows file modes do not provide Unix ACL isolation. Use your account's protected workspace for temporary configs.
- Delivery retries cannot guarantee exactly-once Telegram messages. Network failures after provider acceptance may create duplicates.
- Local source access can expose system logs. The installer grants journal/adm group access; source plugins are trusted code.

Report vulnerabilities privately to `studio.rooni@gmail.com`. Include affected version, reproduction and impact. Do not post real credentials or server addresses in public issues.
