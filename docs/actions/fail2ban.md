# Fail2ban actions

Optional ban/unban adapter for an existing `sshd` jail. Collection and notification delivery work without it.

On a node, from the installed release's `source` directory:

```sh
sudo bash scripts/install-actions.sh enable
```

Use `disable` to undo it. Commands use the jail's configured ban duration. The root-owned policy `/etc/gatewatch/actions-policy.json` protects server/admin addresses; update it when your admin IP changes. A socket-activated helper runs only while handling a request.

Only configured notification owners can request actions. The adapter never executes arbitrary shell commands or changes VPN routes. [Security model](../../SECURITY.md).
