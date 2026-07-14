#!/usr/bin/env bash
set -euo pipefail

# Deletes Bitcoin Core chainstate and rebuilds it from available block data.
# Intended for scheduled maintenance runs (e.g. daily at 03:00 Asia/Hong_Kong).

BITCOIN_DATA_DIR="${BITCOIN_DATA_DIR:-/root/.bitcoin}"
BITCOIN_CLI="${BITCOIN_CLI:-/usr/local/bin/bitcoin-cli}"
BITCOIND_BIN="${BITCOIND_BIN:-/usr/local/bin/bitcoind}"
GENERATOR_SERVICE="${GENERATOR_SERVICE:-bitcoin-wallet-generator.service}"
LOCKFILE="${LOCKFILE:-/tmp/chainstate-rebuild.lock}"

log() {
  printf '[%s] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"
}

cleanup_and_exit() {
  log "ERROR: $*"
  exit 1
}

exec 9>"$LOCKFILE"
if ! flock -n 9; then
  log "Another chainstate rebuild run is already active. Exiting."
  exit 0
fi

CHAINSTATE_DIR="${BITCOIN_DATA_DIR}/chainstate"
BLOCKS_DIR="${BITCOIN_DATA_DIR}/blocks"
MEMPOOL_FILE="${BITCOIN_DATA_DIR}/mempool.dat"

[[ -d "$BITCOIN_DATA_DIR" ]] || cleanup_and_exit "Bitcoin data dir not found: $BITCOIN_DATA_DIR"
[[ -d "$BLOCKS_DIR" ]] || cleanup_and_exit "Blocks dir not found: $BLOCKS_DIR"

if [[ ! -d "$CHAINSTATE_DIR" ]]; then
  log "No chainstate directory found. Nothing to rebuild."
  exit 0
fi

if systemctl is-active --quiet "$GENERATOR_SERVICE"; then
  log "Stopping $GENERATOR_SERVICE"
  systemctl stop "$GENERATOR_SERVICE"
fi

if "$BITCOIN_CLI" -datadir="$BITCOIN_DATA_DIR" getblockchaininfo >/dev/null 2>&1; then
  log "Stopping bitcoind via RPC"
  "$BITCOIN_CLI" -datadir="$BITCOIN_DATA_DIR" stop || true
else
  log "bitcoind RPC unavailable, attempting best-effort stop via process signal"
  pkill -TERM -x bitcoind || true
fi

for _ in $(seq 1 120); do
  if ! pgrep -x bitcoind >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
if pgrep -x bitcoind >/dev/null 2>&1; then
  cleanup_and_exit "bitcoind did not stop within 120 seconds."
fi

log "Deleting chainstate: $CHAINSTATE_DIR"
rm -rf -- "$CHAINSTATE_DIR"
mkdir -p "$CHAINSTATE_DIR"

if [[ -f "$MEMPOOL_FILE" ]]; then
  log "Removing stale mempool file: $MEMPOOL_FILE"
  rm -f -- "$MEMPOOL_FILE"
fi

log "Starting bitcoind with datadir $BITCOIN_DATA_DIR"
started=0
for _ in $(seq 1 60); do
  if output=$("$BITCOIND_BIN" -datadir="$BITCOIN_DATA_DIR" -daemon 2>&1); then
    started=1
    break
  fi
  if [[ "$output" == *"Cannot obtain a lock on directory"* ]]; then
    sleep 2
    continue
  fi
  cleanup_and_exit "Failed to start bitcoind: $output"
done
if [[ "$started" -ne 1 ]]; then
  cleanup_and_exit "bitcoind datadir lock was not released in time."
fi

log "Waiting for RPC readiness"
"$BITCOIN_CLI" -datadir="$BITCOIN_DATA_DIR" -rpcwait getblockchaininfo >/dev/null

log "Restarting $GENERATOR_SERVICE"
systemctl restart "$GENERATOR_SERVICE"

log "Chainstate rebuild cycle started successfully."
