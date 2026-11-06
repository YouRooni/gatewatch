#!/usr/bin/env bash
set -euo pipefail
umask 077
mode=${1:-}
if [[ $EUID -ne 0 || ( "$mode" != enable && "$mode" != disable ) ]]; then
    echo 'Usage: sudo bash scripts/install-pam.sh enable|disable' >&2
    exit 2
fi
source_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
test -f /etc/gatewatch/agent.secret.json
test -f /etc/pam.d/sshd
/usr/sbin/sshd -t
install -d -m 700 /var/backups/gatewatch
cp -a /etc/pam.d/sshd "/var/backups/gatewatch/sshd.pam.$(date +%s%N)"
cp -a /etc/gatewatch/agent.secret.json "/var/backups/gatewatch/agent.secret.$(date +%s%N).json"
if [[ "$mode" == enable ]]; then
    if ! /usr/sbin/sshd -T | grep -q '^usepam yes$'; then
        echo 'UsePAM must already be enabled. SSH configuration was not changed.' >&2
        exit 1
    fi
    python3 - <<'PY'
import re
from pathlib import Path
data = Path('/etc/pam.d/common-auth').read_text()
modules = set(re.findall(r'\b(pam_\w+\.so)\b', '\n'.join(line for line in data.splitlines() if not line.lstrip().startswith('#'))))
if not modules <= {'pam_unix.so', 'pam_deny.so', 'pam_permit.so', 'pam_cap.so'}:
    raise SystemExit('Custom PAM auth stack detected. Review the integration manually; no hook was installed.')
PY
    if ! command -v cc >/dev/null || ! test -f /usr/include/security/pam_modules.h; then
        apt-get update -qq
        apt-get install -y --no-install-recommends gcc libc6-dev libpam0g-dev
    fi
    build=$(mktemp -d)
    trap 'rm -f -- "$build/pam_gatewatch.so"; rmdir -- "$build"' EXIT
    cc -shared -fPIC -O2 -Wall -Wextra -Werror -fstack-protector-strong -D_FORTIFY_SOURCE=2 \
      -Wl,-z,relro,-z,now "$source_root/integrations/pam/pam_gatewatch.c" -lpam -o "$build/pam_gatewatch.so"
    install -d -o root -g root -m 0755 /usr/local/lib/security
    install -o root -g root -m 0644 "$build/pam_gatewatch.so" /usr/local/lib/security/pam_gatewatch.so
fi
GATEWATCH_PAM_MODE="$mode" python3 - <<'PY'
import json
import os
import re
from pathlib import Path
mode = os.environ['GATEWATCH_PAM_MODE']
pam = Path('/etc/pam.d/sshd')
original = pam.read_text()
hook = 'auth optional /usr/local/lib/security/pam_gatewatch.so'
data = '\n'.join(line for line in original.splitlines() if line.strip() != hook) + '\n'
if mode == 'enable':
    if len(re.findall(r'(?m)^@include\s+common-auth\s*$', data)) != 1:
        raise SystemExit('Expected one common-auth include; PAM was not changed.')
    data = re.sub(r'(?m)^@include\s+common-auth\s*$', hook + '\n@include common-auth', data, count=1)
config_path = Path('/etc/gatewatch/agent.secret.json')
config = json.loads(config_path.read_text())
sources = [source for source in config.get('sources', {}).values() if source.get('type') == 'journal']
if len(sources) != 1:
    raise SystemExit('Expected one journal source; PAM was not changed.')
sources[0]['password_capture'] = mode == 'enable'
config_path.write_text(json.dumps(config, indent=2) + '\n')
pam.write_text(data)
PY
install -o root -g root -m 0644 "$source_root/deploy/gatewatch-agent.service" /etc/systemd/system/gatewatch-agent.service
systemctl daemon-reload
systemctl restart gatewatch-agent
systemctl is-active gatewatch-agent
echo "SSH password capture: $mode. Existing authentication rules are preserved; the hook always returns PAM_IGNORE."
