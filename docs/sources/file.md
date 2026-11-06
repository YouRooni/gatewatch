# File

Reads appended Fail2ban log entries from a file. This adapter parses Fail2ban records; arbitrary application logs need a custom parser or [structured events](custom.md).

```json
{"sources":{"fail2ban":{"type":"file","path":"/var/log/fail2ban.log","poll_seconds":2}}}
```

Grant the service account read access. The first run starts at the file tail; offsets persist across restarts. Rotation and truncation are detected. Incomplete lines wait for a newline; lines above 64 KiB are discarded.
