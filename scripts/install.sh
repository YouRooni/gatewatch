#!/usr/bin/env bash
set -euo pipefail
umask 077

role=${1:-}
if [[ "$role" != hub && "$role" != agent ]]; then
    echo 'Usage: sudo ./scripts/install.sh hub|agent [agent.secret.json]'
    exit 2
fi
if [[ $EUID -ne 0 ]]; then
    echo 'Run as root.' >&2
    exit 1
fi
command -v systemctl >/dev/null || { echo 'systemd is required.' >&2; exit 1; }
source_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
config="/etc/gatewatch/$role.secret.json"
if [[ -e "/etc/systemd/system/gatewatch-$role.service" ]]; then
    echo 'This role is already installed. See docs/operations.md for upgrades.' >&2
    exit 1
fi
if [[ "$role" == agent && ! -f "$config" && ! -f "${2:-}" ]]; then
    echo 'Provide the agent config created by: gatewatch nodes add' >&2
    exit 1
fi
if [[ -f /etc/os-release ]]; then
    . /etc/os-release
    case "${ID:-} ${ID_LIKE:-}" in
        *debian*|*ubuntu*) ;;
        *) echo 'Automatic installer supports Debian/Ubuntu. See docs/operations.md for manual setup.' >&2; exit 1 ;;
    esac
fi
apt-get update -qq
apt-get install -y --no-install-recommends python3 python3-venv ca-certificates
python3 -c 'import sys; assert sys.version_info >= (3,10), "Python 3.10+ required"'
id gatewatch >/dev/null 2>&1 || useradd --system --home-dir /var/lib/gatewatch --shell /usr/sbin/nologin gatewatch
install -d -o root -g gatewatch -m 0750 /etc/gatewatch
install -d -o gatewatch -g gatewatch -m 0750 /var/lib/gatewatch
if [[ ! -e /opt/gatewatch ]]; then
    python3 -m venv /opt/gatewatch
    chmod 0755 /opt/gatewatch
    /opt/gatewatch/bin/pip install --disable-pip-version-check "$source_root"
    chmod -R a+rX /opt/gatewatch
else
    /opt/gatewatch/bin/gatewatch --version
fi
if [[ "$role" == hub && ! -f "$config" ]]; then
    /opt/gatewatch/bin/gatewatch --config "$config" init
elif [[ "$role" == agent && ! -f "$config" ]]; then
    install -o root -g gatewatch -m 0640 -- "$2" "$config"
fi
chown root:gatewatch "$config"
chmod 0640 "$config"
/opt/gatewatch/bin/gatewatch --config "$config" check
if [[ "$role" == agent ]]; then
    usermod -a -G systemd-journal gatewatch
    if getent group adm >/dev/null; then
        usermod -a -G adm gatewatch
    fi
fi
install -o root -g root -m 0644 "$source_root/deploy/gatewatch-$role.service" "/etc/systemd/system/gatewatch-$role.service"
systemctl daemon-reload
systemctl enable --now "gatewatch-$role.service"
echo "Gatewatch $role installed. Logs: journalctl -u gatewatch-$role -f"
