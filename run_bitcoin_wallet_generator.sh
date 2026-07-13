#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [[ -f ".venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source ".venv/bin/activate"
fi

if [[ -f "telegram.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "telegram.env"
  set +a
fi

exec python3 bitcoin_wallet_generator.py \
  --output "$SCRIPT_DIR/generated_wallets.jsonl" \
  --state-file "$SCRIPT_DIR/wallet_generator_state.json" \
  --max-load-per-cpu 0.60 \
  --poll-seconds 5 \
  --telegram-summary-interval-seconds 3600 \
  --storage-limit-gb 85 \
  --balance-mode local-node \
  --bitcoin-cli-path bitcoin-cli
