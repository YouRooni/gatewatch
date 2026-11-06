# Journal

The default Linux source reads SSH authentication and Fail2ban events from journald. Requires `journalctl` and journal access for the service account.

```json
{"sources":{"journal":{"type":"journal"}}}
```

Starts at the current journal tail. Saved cursors survive restarts; expired cursors resume from the last timestamp within seven days. Log retention limits recovery.

For Fail2ban, set `logtarget = SYSLOG` in `/etc/fail2ban/fail2ban.d/gatewatch.local` and apply `fail2ban-client set logtarget SYSLOG`. Do not collect the same entries through a file source too.

[Optional failed-password capture](../integrations/pam.md).
