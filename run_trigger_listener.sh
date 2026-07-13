#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

source "${SCRIPT_DIR}/.venv/bin/activate"

set -a
source "${SCRIPT_DIR}/telegram.env"
set +a

python3 "${SCRIPT_DIR}/telegram_trigger_listener.py" "$@"
