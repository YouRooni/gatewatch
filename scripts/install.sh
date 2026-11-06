#!/usr/bin/env bash
set -euo pipefail
source_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
role=${1:-}
if [[ "$role" != hub && "$role" != agent && "$role" != demo ]]; then
    echo 'Usage: sudo bash scripts/install.sh hub|agent|demo [agent.secret.json] [options]' >&2
    exit 2
fi
shift
config=()
if [[ $# -gt 0 && "$1" != --* ]]; then
    config=(--config "$1")
    shift
fi
source_args=(--source "$source_root")
for argument in "$@"; do
    case "$argument" in
        --version|--version=*|--source|--source=*|--bundle|--bundle=*) source_args=() ;;
    esac
done
exec python3 "$source_root/scripts/manage.py" install "$role" "${source_args[@]}" "${config[@]}" "$@"
