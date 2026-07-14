#!/usr/bin/env bash
set -euo pipefail

# Deploy/redeploy Bitcoin Core with:
# - prune target ~7 GiB (prune=7000)
# - strict OS-level 50 GiB cap via loop-mounted filesystem
# - restart wallet generator service so /check uses local node mode
# - optional cron registration for daily destructive reset at 03:00 HKT

BITCOIN_CORE_VERSION="${BITCOIN_CORE_VERSION:-31.1}"
IMAGE_PATH="${IMAGE_PATH:-/root/bitcoin50.img}"
MOUNT_POINT="${MOUNT_POINT:-/mnt/bitcoin50}"
DATA_LINK="${DATA_LINK:-/root/.bitcoin}"
DATA_DIR_TARGET="${DATA_DIR_TARGET:-$MOUNT_POINT}"
REPO_DIR="${REPO_DIR:-/root/stocksignal}"
GENERATOR_SERVICE="${GENERATOR_SERVICE:-bitcoin-wallet-generator.service}"
BITCOIND_BIN="${BITCOIND_BIN:-/usr/local/bin/bitcoind}"
BITCOIN_CLI="${BITCOIN_CLI:-/usr/local/bin/bitcoin-cli}"
RESET_DATA=0
SKIP_CRON=0

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

die() {
  log "ERROR: $*"
  exit 1
}

usage() {
  cat <<'USAGE'
Usage: deploy_bitcoin_node.sh [--reset-data] [--skip-cron]

Options:
  --reset-data   Wipe existing Bitcoin data mount/image before deploy.
  --skip-cron    Do not modify root crontab.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --reset-data)
      RESET_DATA=1
      shift
      ;;
    --skip-cron)
      SKIP_CRON=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "Unknown argument: $1"
      ;;
  esac
done

[[ "$(id -u)" -eq 0 ]] || die "Run as root."

install_bitcoin_core() {
  if [[ -x "$BITCOIND_BIN" && -x "$BITCOIN_CLI" ]]; then
    log "Bitcoin Core binaries already installed."
    return
  fi

  log "Installing Bitcoin Core $BITCOIN_CORE_VERSION binaries."
  apt update
  apt install -y curl tar xz-utils

  local tmp_dir archive sums
  tmp_dir="$(mktemp -d)"
  archive="bitcoin-${BITCOIN_CORE_VERSION}-x86_64-linux-gnu.tar.gz"
  sums="SHA256SUMS"

  curl -fsSL "https://bitcoincore.org/bin/bitcoin-core-${BITCOIN_CORE_VERSION}/${archive}" -o "${tmp_dir}/${archive}"
  curl -fsSL "https://bitcoincore.org/bin/bitcoin-core-${BITCOIN_CORE_VERSION}/${sums}" -o "${tmp_dir}/${sums}"

  local expected actual
  expected="$(rg "${archive}" "${tmp_dir}/${sums}" | awk '{print $1}')"
  actual="$(sha256sum "${tmp_dir}/${archive}" | awk '{print $1}')"
  [[ -n "$expected" ]] || die "Could not find checksum entry for ${archive}."
  [[ "$expected" == "$actual" ]] || die "Checksum mismatch for ${archive}."

  tar -xzf "${tmp_dir}/${archive}" -C "$tmp_dir"
  install -m 0755 -t /usr/local/bin \
    "${tmp_dir}/bitcoin-${BITCOIN_CORE_VERSION}/bin/bitcoind" \
    "${tmp_dir}/bitcoin-${BITCOIN_CORE_VERSION}/bin/bitcoin-cli"
  rm -rf "$tmp_dir"
}

stop_runtime() {
  if systemctl is-active --quiet "$GENERATOR_SERVICE"; then
    log "Stopping ${GENERATOR_SERVICE}"
    systemctl stop "$GENERATOR_SERVICE"
  fi

  if "$BITCOIN_CLI" -datadir="$DATA_LINK" getblockchaininfo >/dev/null 2>&1; then
    log "Stopping bitcoind via RPC."
    "$BITCOIN_CLI" -datadir="$DATA_LINK" stop || true
  else
    pkill -TERM -x bitcoind || true
  fi

  for _ in $(seq 1 90); do
    if ! pgrep -x bitcoind >/dev/null 2>&1; then
      return
    fi
    sleep 1
  done
  pkill -9 -x bitcoind || true
}

prepare_mount() {
  mkdir -p "$MOUNT_POINT"

  if [[ "$RESET_DATA" -eq 1 ]]; then
    log "Reset requested; removing old Bitcoin mount/image/data."
    safe_unmount "$MOUNT_POINT"
    rm -f "$IMAGE_PATH"
    if ! mountpoint -q "$MOUNT_POINT"; then
      rm -rf "$MOUNT_POINT"
    fi
    if [[ -L "$DATA_LINK" || -f "$DATA_LINK" ]]; then
      rm -f "$DATA_LINK"
    elif [[ -d "$DATA_LINK" ]]; then
      rm -rf "$DATA_LINK"
    fi
    rm -rf /root/.bitcoin.bak*
    mkdir -p "$MOUNT_POINT"
  fi

  if [[ ! -f "$IMAGE_PATH" ]]; then
    log "Creating 50 GiB image at ${IMAGE_PATH}."
    fallocate -l 50G "$IMAGE_PATH"
    mkfs.ext4 -F "$IMAGE_PATH"
  fi

  if ! mount | rg -q " ${MOUNT_POINT} "; then
    log "Mounting loop filesystem at ${MOUNT_POINT}."
    mount -o loop "$IMAGE_PATH" "$MOUNT_POINT"
  fi

  if [[ -e "$DATA_LINK" && ! -L "$DATA_LINK" ]]; then
    local backup_dir
    backup_dir="/root/.bitcoin.bak.$(date +%s)"
    log "Moving existing ${DATA_LINK} to ${backup_dir}."
    mv "$DATA_LINK" "$backup_dir"
  fi

  if [[ -L "$DATA_LINK" ]]; then
    rm -f "$DATA_LINK"
  fi
  ln -s "$DATA_DIR_TARGET" "$DATA_LINK"

  if ! rg -q "${IMAGE_PATH} ${MOUNT_POINT} ext4 loop,defaults 0 0" /etc/fstab; then
    log "Registering loop mount in /etc/fstab."
    printf '%s %s ext4 loop,defaults 0 0\n' "$IMAGE_PATH" "$MOUNT_POINT" >> /etc/fstab
  fi
}

write_bitcoin_conf() {
  mkdir -p "$DATA_LINK"
  cat > "${DATA_LINK}/bitcoin.conf" <<'EOF'
server=1
daemon=1
txindex=0
prune=7000
rpcbind=127.0.0.1
rpcallowip=127.0.0.1
EOF
}

start_runtime() {
  log "Starting bitcoind."
  "$BITCOIND_BIN" -datadir="$DATA_LINK" -daemon=1
  log "Waiting for bitcoind process and RPC cookie."
  local cookie_path
  cookie_path="${DATA_LINK}/.cookie"
  for _ in $(seq 1 120); do
    if ! pgrep -x bitcoind >/dev/null 2>&1; then
      tail -n 40 "${DATA_LINK}/debug.log" 2>/dev/null || true
      die "bitcoind exited before RPC became ready."
    fi
    if [[ -f "$cookie_path" ]]; then
      break
    fi
    sleep 1
  done
  [[ -f "$cookie_path" ]] || die "RPC cookie was not created in time: ${cookie_path}"

  log "Waiting for RPC readiness."
  "$BITCOIN_CLI" -datadir="$DATA_LINK" -rpcwait getblockchaininfo >/dev/null

  if systemctl list-unit-files | rg -q "^${GENERATOR_SERVICE}"; then
    log "Restarting ${GENERATOR_SERVICE}."
    systemctl restart "$GENERATOR_SERVICE"
  else
    log "Generator service not found; skipping restart."
  fi
}

install_daily_cron() {
  [[ "$SKIP_CRON" -eq 0 ]] || return
  [[ -x "${REPO_DIR}/reset_bitcoin_node_daily.sh" ]] || return

  local existing tmp
  existing="$(mktemp)"
  tmp="$(mktemp)"
  crontab -l -u root > "$existing" 2>/dev/null || true

  rg -v "reset_bitcoin_node_daily.sh|CRON_TZ=Asia/Hong_Kong" "$existing" > "$tmp" || true
  {
    cat "$tmp"
    echo "CRON_TZ=Asia/Hong_Kong"
    echo "0 3 * * * ${REPO_DIR}/reset_bitcoin_node_daily.sh >> /var/log/bitcoin-node-reset.log 2>&1"
  } | crontab -u root -
  rm -f "$existing" "$tmp"
  log "Installed daily 03:00 HKT reset cron."
}

main() {
  install_bitcoin_core
  stop_runtime
  prepare_mount
  write_bitcoin_conf
  start_runtime
  install_daily_cron
  log "Bitcoin node deploy complete (7 GiB prune target, 50 GiB OS cap)."
}

main "$@"
