#!/usr/bin/env bash
set -euo pipefail

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

log() {
  printf '[%s] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"
}

die() {
  log "ERROR: $*"
  exit 1
}

[[ "$(id -u)" -eq 0 ]] || die "Run as root."
[[ -x "$DEPLOY_SCRIPT" ]] || die "Deploy script not found or not executable: ${DEPLOY_SCRIPT}"

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
rm -f "$DATA_LINK"
umount "$MOUNT_POINT" 2>/dev/null || true
rm -f "$IMAGE_PATH"
rm -rf "$MOUNT_POINT"
rm -rf /root/.bitcoin.bak*

log "Re-deploying fresh node."
"$DEPLOY_SCRIPT" --reset-data

log "Daily node reset complete."
