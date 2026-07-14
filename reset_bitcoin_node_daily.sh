#!/usr/bin/env bash
set -eEuo pipefail

# Daily destructive reset workflow:
# - stop generator + bitcoind
# - delete node data/image/mount and backups
# - redeploy node with prune=7000 and 50 GiB loop mount cap

REPO_DIR="${REPO_DIR:-/root/stocksignal}"
LOCKFILE="${LOCKFILE:-/tmp/bitcoin-node-daily-reset.lock}"
DEPLOY_SCRIPT="${DEPLOY_SCRIPT:-${REPO_DIR}/deploy_bitcoin_node.sh}"
IMAGE_PATH="${IMAGE_PATH:-/root/bitcoin50.img}"
MOUNT_POINT="${MOUNT_POINT:-/mnt/bitcoin50}"
DATA_LINK="${DATA_LINK:-/root/.bitcoin}"
GENERATOR_SERVICE="${GENERATOR_SERVICE:-bitcoin-wallet-generator.service}"
BITCOIN_CLI="${BITCOIN_CLI:-/usr/local/bin/bitcoin-cli}"
TELEGRAM_ENV_FILE="${TELEGRAM_ENV_FILE:-${REPO_DIR}/telegram.env}"
HOST_TAG="${HOST_TAG:-$(hostname)}"
START_EPOCH="$(date +%s)"

log() {
  printf '[%s] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"
}

safe_unmount() {
  local target="$1"
  if ! mountpoint -q "$target"; then
    return
  fi

  for _ in $(seq 1 10); do
    if umount "$target" 2>/dev/null; then
      return
    fi
    sleep 1
  done

  log "Regular unmount failed for ${target}; trying lazy unmount."
  umount -l "$target"
}

load_telegram_env() {
  if [[ -f "$TELEGRAM_ENV_FILE" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$TELEGRAM_ENV_FILE"
    set +a
  fi
}

send_telegram_alert() {
  local text="$1"
  local token chat_id
  token="${TELEGRAM_BOT_TOKEN:-}"
  chat_id="${TELEGRAM_CHAT_ID:-}"
  if [[ -z "$token" || -z "$chat_id" ]]; then
    return
  fi
  curl -sS -X POST "https://api.telegram.org/bot${token}/sendMessage" \
    -d "chat_id=${chat_id}" \
    --data-urlencode "text=${text}" >/dev/null || true
}

on_error() {
  local line_no="$1"
  local elapsed
  elapsed="$(( $(date +%s) - START_EPOCH ))"
  send_telegram_alert "Bitcoin node daily reset FAILED on ${HOST_TAG} at line ${line_no} after ${elapsed}s."
}

die() {
  log "ERROR: $*"
  exit 1
}

trap 'on_error $LINENO' ERR

[[ "$(id -u)" -eq 0 ]] || die "Run as root."
[[ -x "$DEPLOY_SCRIPT" ]] || die "Deploy script not found or not executable: ${DEPLOY_SCRIPT}"
load_telegram_env

exec 9>"$LOCKFILE"
if ! flock -n 9; then
  log "Another reset run is active. Exiting."
  exit 0
fi

if systemctl is-active --quiet "$GENERATOR_SERVICE"; then
  log "Stopping ${GENERATOR_SERVICE}"
  systemctl stop "$GENERATOR_SERVICE"
fi

if "$BITCOIN_CLI" -datadir="$DATA_LINK" getblockchaininfo >/dev/null 2>&1; then
  log "Stopping bitcoind via RPC"
  "$BITCOIN_CLI" -datadir="$DATA_LINK" stop || true
else
  pkill -TERM -x bitcoind || true
fi

for _ in $(seq 1 120); do
  if ! pgrep -x bitcoind >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
pkill -9 -x bitcoind || true

log "Deleting Bitcoin data link/image/mount and backups."
if [[ -L "$DATA_LINK" || -f "$DATA_LINK" ]]; then
  rm -f "$DATA_LINK"
elif [[ -d "$DATA_LINK" ]]; then
  rm -rf "$DATA_LINK"
fi
safe_unmount "$MOUNT_POINT"
rm -f "$IMAGE_PATH"
if ! mountpoint -q "$MOUNT_POINT"; then
  rm -rf "$MOUNT_POINT"
fi
rm -rf /root/.bitcoin.bak*

log "Re-deploying fresh node."
"$DEPLOY_SCRIPT" --reset-data

log "Daily node reset complete."
elapsed="$(( $(date +%s) - START_EPOCH ))"
send_telegram_alert "Bitcoin node daily reset succeeded on ${HOST_TAG} in ${elapsed}s."
