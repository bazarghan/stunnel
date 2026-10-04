#!/usr/bin/env bash
# Compatibility with local binary/config installation.
set -euo pipefail
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
if [[ $# == 2 && $1 != --* ]]; then
    exec bash "$script_dir/../install.sh" --binary "$1" --config "$2" --no-menu
fi
exec bash "$script_dir/../install.sh" "$@"
