#!/usr/bin/env bash
set -euo pipefail
umask 077
mode=${1:-}
if [[ $EUID -ne 0 || ( "$mode" != enable && "$mode" != disable ) ]]; then
    echo 'Usage: sudo bash scripts/install-actions.sh enable|disable' >&2
    exit 2
fi
source_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
test -f /etc/gatewatch/agent.secret.json
install -d -m 700 /var/backups/gatewatch
cp -a /etc/gatewatch/agent.secret.json "/var/backups/gatewatch/agent.actions.$(date +%s%N).secret.json"
if [[ "$mode" == enable ]]; then
    fail2ban-client status sshd >/dev/null
    GATEWATCH_ADMIN_CONNECTION="${SSH_CONNECTION:-}" python3 - <<'PY'
import ipaddress
import json
import os
import re
import subprocess
from pathlib import Path
policy = Path('/etc/gatewatch/actions-policy.json')
if not policy.exists():
    trusted = {'127.0.0.0/8','::1/128'}
    connection = os.environ.get('GATEWATCH_ADMIN_CONNECTION','').split()
    if connection:
        trusted.add(str(ipaddress.ip_network(connection[0],strict=False)))
    addresses = json.loads(subprocess.check_output(['ip','-j','address','show']))
    for interface in addresses:
        for address in interface.get('addr_info',[]):
            trusted.add(str(ipaddress.ip_network(address['local'],strict=False)))
    ignored = subprocess.check_output(['fail2ban-client','get','sshd','ignoreip'],text=True)
    for candidate in re.findall(r'[0-9a-fA-F:.]+(?:/\d+)?',ignored):
        try:
            trusted.add(str(ipaddress.ip_network(candidate,strict=False)))
        except ValueError:
            pass
    fd = os.open(policy,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as target:
        json.dump({'trusted_ips':sorted(trusted)},target,indent=2)
PY
    install -d -o root -g root -m 0755 /usr/local/libexec
    install -o root -g root -m 0644 "$source_root/integrations/actions/helper.py" /usr/local/libexec/gatewatch-action-helper.py
    install -o root -g root -m 0644 "$source_root/deploy/gatewatch-actions.socket" /etc/systemd/system/gatewatch-actions.socket
    install -o root -g root -m 0644 "$source_root/deploy/gatewatch-actions@.service" /etc/systemd/system/gatewatch-actions@.service
    systemctl daemon-reload
    systemctl enable --now gatewatch-actions.socket
else
    systemctl disable --now gatewatch-actions.socket || true
fi
GATEWATCH_ACTIONS_MODE="$mode" python3 - <<'PY'
import json
import os
from pathlib import Path
p = Path('/etc/gatewatch/agent.secret.json')
config = json.loads(p.read_text())
if os.environ['GATEWATCH_ACTIONS_MODE']=='enable':
    config['actions'] = {'type':'fail2ban'}
else:
    config.pop('actions',None)
p.write_text(json.dumps(config,indent=2)+'\n')
PY
systemctl restart gatewatch-agent
echo "Remote Fail2ban actions: $mode"
