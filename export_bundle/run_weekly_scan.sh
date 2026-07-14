#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Load virtual environment
source "${SCRIPT_DIR}/.venv/bin/activate"

# Export Telegram credentials from repo env file
set -a
source "${SCRIPT_DIR}/telegram.env"
set +a

python3 "${SCRIPT_DIR}/weekly_hk_stock_alert.py" "$@"
