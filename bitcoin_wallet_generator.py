#!/usr/bin/env python3
"""Continuously generate Bitcoin wallets with random 12-word seed phrases."""

from __future__ import annotations

import argparse
import decimal
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

try:
    from bip_utils import (
        Bip39MnemonicGenerator,
        Bip39SeedGenerator,
        Bip39WordsNum,
        Bip44,
        Bip44Changes,
        Bip44Coins,
    )
except ImportError as exc:  # pragma: no cover - import guard
    raise SystemExit(
        "Missing dependency 'bip_utils'. Install with: pip install -r requirements.txt"
    ) from exc


def normalized_load() -> float:
    """Return current 1-minute load normalized by CPU core count."""
    if not hasattr(os, "getloadavg"):
        return 0.0

    load_1m, _, _ = os.getloadavg()
    cpu_count = os.cpu_count() or 1
    return load_1m / cpu_count


def load_state(state_file: Path) -> dict[str, Any]:
    if not state_file.exists():
        return {}
    try:
        with state_file.open("r", encoding="utf-8") as f:
            payload = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}
    return payload if isinstance(payload, dict) else {}


def save_state(state_file: Path, payload: dict[str, Any]) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    temp_file = state_file.with_suffix(f"{state_file.suffix}.tmp")
    with temp_file.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=True, sort_keys=True)
    temp_file.replace(state_file)


def generate_wallet(include_private_key: bool) -> dict[str, Any]:
    """Create one Bitcoin wallet using a random 12-word mnemonic."""
    mnemonic = Bip39MnemonicGenerator().FromWordsNumber(Bip39WordsNum.WORDS_NUM_12)
    seed_bytes = Bip39SeedGenerator(mnemonic).Generate()
    account = (
        Bip44.FromSeed(seed_bytes, Bip44Coins.BITCOIN)
        .Purpose()
        .Coin()
        .Account(0)
        .Change(Bip44Changes.CHAIN_EXT)
        .AddressIndex(0)
    )

    wallet: dict[str, Any] = {
        "created_at_utc": datetime.now(tz=timezone.utc).isoformat(),
        "address": account.PublicKey().ToAddress(),
        "mnemonic": str(mnemonic),
        "derivation_path": "m/44'/0'/0'/0/0",
    }
    if include_private_key:
        wallet["wif_private_key"] = account.PrivateKey().ToWif()
    return wallet


def write_jsonl(output_file: Path, payload: dict[str, Any]) -> None:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    with output_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=True) + "\n")


def truncate_backlog(output_file: Path) -> None:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    with output_file.open("w", encoding="utf-8"):
        pass


def backlog_size_bytes(output_file: Path) -> int:
    if not output_file.exists():
        return 0
    return output_file.stat().st_size


def run_bitcoin_cli(
    bitcoin_cli_path: str, args: list[str], wallet_name: str | None = None
) -> Any:
    cmd = [bitcoin_cli_path]
    if wallet_name:
        cmd.append(f"-rpcwallet={wallet_name}")
    cmd.extend(args)

    completed = subprocess.run(
        cmd,
        check=True,
        capture_output=True,
        text=True,
    )
    stdout = completed.stdout.strip()
    if not stdout:
        return {}
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        return stdout


def wallet_exists(bitcoin_cli_path: str, wallet_name: str) -> bool:
    payload = run_bitcoin_cli(bitcoin_cli_path, ["listwalletdir"])
    wallets = payload.get("wallets", []) if isinstance(payload, dict) else []
    return any(item.get("name") == wallet_name for item in wallets if isinstance(item, dict))


def wallet_loaded(bitcoin_cli_path: str, wallet_name: str) -> bool:
    payload = run_bitcoin_cli(bitcoin_cli_path, ["listwallets"])
    return isinstance(payload, list) and wallet_name in payload


def ensure_local_watchonly_wallet(bitcoin_cli_path: str, wallet_name: str) -> None:
    if wallet_loaded(bitcoin_cli_path, wallet_name):
        return

    if wallet_exists(bitcoin_cli_path, wallet_name):
        run_bitcoin_cli(bitcoin_cli_path, ["loadwallet", wallet_name])
        return

    run_bitcoin_cli(
        bitcoin_cli_path,
        [
            "-named",
            "createwallet",
            f"wallet_name={wallet_name}",
            "disable_private_keys=true",
            "blank=true",
            "descriptors=true",
            "load_on_startup=true",
        ],
    )


def descriptor_for_address(bitcoin_cli_path: str, address: str) -> str:
    descriptor_payload = run_bitcoin_cli(
        bitcoin_cli_path,
        ["getdescriptorinfo", f"addr({address})"],
    )
    if not isinstance(descriptor_payload, dict):
        raise RuntimeError("Could not build descriptor for address.")
    descriptor = str(descriptor_payload.get("descriptor", "")).strip()
    if not descriptor:
        raise RuntimeError("Missing descriptor in getdescriptorinfo response.")
    return descriptor


def import_addresses_to_local_wallet(
    bitcoin_cli_path: str,
    wallet_name: str,
    addresses: list[str],
) -> int:
    if not addresses:
        return 0

    requests_payload = []
    for address in addresses:
        requests_payload.append(
            {
                "desc": descriptor_for_address(bitcoin_cli_path, address),
                "timestamp": "now",
                "active": False,
                "internal": False,
                "label": "walletgen",
            }
        )

    results = run_bitcoin_cli(
        bitcoin_cli_path,
        [
            "importdescriptors",
            json.dumps(requests_payload, ensure_ascii=True),
        ],
        wallet_name=wallet_name,
    )
    if not isinstance(results, list):
        return 0

    imported = 0
    for item in results:
        if isinstance(item, dict) and item.get("success", False):
            imported += 1
    return imported


def read_new_addresses_from_backlog(
    output_file: Path,
    start_offset: int,
    max_records: int,
) -> tuple[list[str], int, bool]:
    if not output_file.exists():
        return [], 0, start_offset > 0

    current_size = output_file.stat().st_size
    was_truncated = start_offset > current_size
    offset = 0 if was_truncated else start_offset

    addresses: list[str] = []
    with output_file.open("r", encoding="utf-8") as f:
        f.seek(offset)
        while len(addresses) < max_records:
            line = f.readline()
            if not line:
                break
            next_offset = f.tell()
            raw_line = line.strip()
            if not raw_line:
                offset = next_offset
                continue

            try:
                payload = json.loads(raw_line)
            except json.JSONDecodeError:
                offset = next_offset
                continue

            if isinstance(payload, dict):
                address = str(payload.get("address", "")).strip()
                if address:
                    addresses.append(address)
            offset = next_offset

    return addresses, offset, was_truncated


def compute_total_balance_via_api(output_file: Path, balance_api_base: str) -> tuple[int, int, int]:
    if not output_file.exists():
        return 0, 0, 0

    addresses: set[str] = set()
    with output_file.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                payload = json.loads(line.strip())
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            address = str(payload.get("address", "")).strip()
            if address:
                addresses.add(address)

    total_sats = 0
    failed_lookups = 0
    for address in sorted(addresses):
        url = f"{balance_api_base.rstrip('/')}/address/{address}"
        try:
            response = requests.get(url, timeout=20)
            response.raise_for_status()
            payload = response.json()
            chain_stats = payload.get("chain_stats", {})
            mempool_stats = payload.get("mempool_stats", {})
            chain_balance = int(chain_stats.get("funded_txo_sum", 0)) - int(
                chain_stats.get("spent_txo_sum", 0)
            )
            mempool_balance = int(mempool_stats.get("funded_txo_sum", 0)) - int(
                mempool_stats.get("spent_txo_sum", 0)
            )
            total_sats += max(0, chain_balance + mempool_balance)
        except Exception:  # pylint: disable=broad-except
            failed_lookups += 1
        time.sleep(0.03)

    return len(addresses), total_sats, failed_lookups


def compute_total_balance_via_local_node(
    bitcoin_cli_path: str,
    wallet_name: str,
) -> tuple[int, int]:
    payload = run_bitcoin_cli(bitcoin_cli_path, ["getbalances"], wallet_name=wallet_name)
    if not isinstance(payload, dict):
        return 0, 0

    watchonly = payload.get("watchonly", {})
    trusted_btc = decimal.Decimal(str(watchonly.get("trusted", 0)))
    pending_btc = decimal.Decimal(str(watchonly.get("untrusted_pending", 0)))
    total_sats = int((trusted_btc + pending_btc) * decimal.Decimal("100000000"))

    tracked = run_bitcoin_cli(
        bitcoin_cli_path,
        ["listreceivedbyaddress", "0", "true", "true"],
        wallet_name=wallet_name,
    )
    tracked_addresses = len(tracked) if isinstance(tracked, list) else 0
    return tracked_addresses, max(0, total_sats)


def send_balance_summary(
    bot_token: str,
    chat_id: str,
    balance_mode: str,
    bitcoin_cli_path: str,
    wallet_name: str,
    output_file: Path,
    balance_api_base: str,
    reason: str,
) -> None:
    if balance_mode == "local-node":
        wallet_count, total_sats = compute_total_balance_via_local_node(
            bitcoin_cli_path=bitcoin_cli_path,
            wallet_name=wallet_name,
        )
        failed_lookups = 0
        source_line = "Source: local-node (Bitcoin Core wallet)"
    else:
        wallet_count, total_sats, failed_lookups = compute_total_balance_via_api(
            output_file=output_file,
            balance_api_base=balance_api_base,
        )
        source_line = f"Source: API ({balance_api_base})"

    message = (
        f"Bitcoin wallet summary ({reason})\n"
        f"Wallets tracked: {wallet_count}\n"
        f"Total BTC: {total_sats / 100_000_000:.8f}\n"
        f"Failed balance checks: {failed_lookups}\n"
        f"{source_line}"
    )
    send_telegram_message(
        bot_token=bot_token,
        chat_id=chat_id,
        text=message,
    )


def send_telegram_message(bot_token: str, chat_id: str, text: str) -> None:
    api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    response = requests.post(
        api_url,
        data={"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"},
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok", False):
        raise RuntimeError(f"Telegram API returned error: {payload}")


def process_bot_commands(
    bot_token: str,
    expected_chat_id: str,
    updates_offset: int,
    output_file: Path,
) -> tuple[int, bool, bool]:
    api_url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
    response = requests.get(
        api_url,
        params={
            "offset": updates_offset,
            "timeout": 0,
            "allowed_updates": json.dumps(["message"]),
        },
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok", False):
        raise RuntimeError(f"Telegram API returned error: {payload}")

    results = payload.get("result", [])
    last_seen_update = updates_offset
    was_cleared = False
    check_requested = False
    for update in results:
        if not isinstance(update, dict):
            continue
        update_id = int(update.get("update_id", 0))
        if update_id >= last_seen_update:
            last_seen_update = update_id + 1

        message = update.get("message") or {}
        chat = message.get("chat") or {}
        message_chat_id = str(chat.get("id", "")).strip()
        text = str(message.get("text", "")).strip()

        if message_chat_id != expected_chat_id or not text:
            continue
        command = text.split()[0].lower()
        if command == "/new":
            truncate_backlog(output_file)
            was_cleared = True
            send_telegram_message(
                bot_token=bot_token,
                chat_id=expected_chat_id,
                text=(
                    "Wallet memory has been reset via /new.\n"
                    "Backlog cleared and fresh wallet generation continues."
                ),
            )
            continue
        if command == "/check":
            check_requested = True
            send_telegram_message(
                bot_token=bot_token,
                chat_id=expected_chat_id,
                text="Manual balance check requested. Preparing summary now.",
            )

    return last_seen_update, was_cleared, check_requested


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Continuously generate Bitcoin wallets with random 12-word seed phrases."
        )
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("./generated_wallets.jsonl"),
        help="JSON Lines file where generated wallet data is appended.",
    )
    parser.add_argument(
        "--interval-seconds",
        type=float,
        default=0.0,
        help="Sleep duration after each generated wallet.",
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=5.0,
        help="Sleep duration between idle-capacity checks.",
    )
    parser.add_argument(
        "--max-load-per-cpu",
        type=float,
        default=0.60,
        help=(
            "Only generate when normalized 1-minute load is <= this value "
            "(e.g. 0.60 means 60%% of one core per core)."
        ),
    )
    parser.add_argument(
        "--ignore-load",
        action="store_true",
        help="Generate continuously and skip VPS idle-capacity checks.",
    )
    parser.add_argument(
        "--include-private-key",
        action="store_true",
        help="Include WIF private key in output (sensitive).",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=0,
        help="Optional number of wallets to generate before exiting (0 = infinite).",
    )
    parser.add_argument(
        "--storage-limit-gb",
        type=float,
        default=85.0,
        help="Pause generation when backlog file reaches this size in GB.",
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=Path("./wallet_generator_state.json"),
        help="State file storing bot offsets and runtime metadata.",
    )
    parser.add_argument(
        "--telegram-summary-interval-seconds",
        type=float,
        default=3600.0,
        help="Seconds between Telegram messages with total BTC across wallets.",
    )
    parser.add_argument(
        "--telegram-poll-seconds",
        type=float,
        default=5.0,
        help="How often to poll Telegram for commands such as /new.",
    )
    parser.add_argument(
        "--telegram-bot-token",
        default=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        help="Telegram bot token (defaults to TELEGRAM_BOT_TOKEN env var).",
    )
    parser.add_argument(
        "--telegram-chat-id",
        default=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        help="Telegram chat ID allowed to control bot (defaults to env var).",
    )
    parser.add_argument(
        "--disable-telegram",
        action="store_true",
        help="Disable all Telegram notifications and command handling.",
    )
    parser.add_argument(
        "--balance-api-base",
        default="https://blockstream.info/api",
        help="Base API URL used to check BTC balances per address.",
    )
    parser.add_argument(
        "--balance-mode",
        choices=("local-node", "api"),
        default="local-node",
        help="Balance source mode. local-node is recommended for 100k+ addresses.",
    )
    parser.add_argument(
        "--bitcoin-cli-path",
        default="bitcoin-cli",
        help="Path to bitcoin-cli executable for local-node mode.",
    )
    parser.add_argument(
        "--watchonly-wallet-base-name",
        default="walletgen_watch",
        help="Base name for local watch-only wallet(s).",
    )
    parser.add_argument(
        "--import-batch-size",
        type=int,
        default=2000,
        help="Addresses imported per local-node sync batch.",
    )
    parser.add_argument(
        "--import-sync-interval-seconds",
        type=float,
        default=2.0,
        help="How often backlog addresses are synced to local node wallet.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    storage_limit_bytes = int(args.storage_limit_gb * (1024**3))
    telegram_enabled = (
        not args.disable_telegram
        and bool(args.telegram_bot_token)
        and bool(args.telegram_chat_id)
    )

    state = load_state(args.state_file)
    generated = int(state.get("generated", 0))
    telegram_offset = int(state.get("telegram_offset", 0))
    storage_limit_notified = bool(state.get("storage_limit_notified", False))
    imported_offset = int(state.get("imported_offset", 0))
    imported_total = int(state.get("imported_total", 0))
    active_wallet_name = str(
        state.get("active_watchonly_wallet", args.watchonly_wallet_base_name)
    ).strip() or args.watchonly_wallet_base_name

    local_node_ready = args.balance_mode != "local-node"
    if args.balance_mode == "local-node":
        try:
            ensure_local_watchonly_wallet(args.bitcoin_cli_path, active_wallet_name)
            local_node_ready = True
        except Exception as exc:  # pylint: disable=broad-except
            local_node_ready = False
            print(f"Local-node setup error ({active_wallet_name}): {exc}")

    next_poll = time.monotonic()
    next_summary = time.monotonic() + max(10.0, args.telegram_summary_interval_seconds)
    next_generation = time.monotonic()
    next_import_sync = time.monotonic()
    next_local_node_retry = time.monotonic()
    manual_summary_requested = False

    print(
        "Starting wallet generation daemon. Press Ctrl+C to stop. "
        f"Output file: {args.output.resolve()}; storage limit: {args.storage_limit_gb} GB"
    )
    if not telegram_enabled:
        print("Telegram features disabled (token/chat-id missing or --disable-telegram set).")

    try:
        while True:
            now = time.monotonic()

            if args.count > 0 and generated >= args.count:
                print(f"Reached requested count ({args.count}). Exiting.")
                break

            if args.balance_mode == "local-node" and (not local_node_ready) and now >= next_local_node_retry:
                try:
                    ensure_local_watchonly_wallet(args.bitcoin_cli_path, active_wallet_name)
                    local_node_ready = True
                    print(f"Local-node wallet ready: {active_wallet_name}")
                except Exception as exc:  # pylint: disable=broad-except
                    print(f"Local-node setup retry failed ({active_wallet_name}): {exc}")
                next_local_node_retry = now + 15.0

            if telegram_enabled and now >= next_poll:
                try:
                    (
                        telegram_offset,
                        was_cleared,
                        check_requested,
                    ) = process_bot_commands(
                        bot_token=args.telegram_bot_token,
                        expected_chat_id=str(args.telegram_chat_id),
                        updates_offset=telegram_offset,
                        output_file=args.output,
                    )
                    manual_summary_requested = manual_summary_requested or check_requested
                    if was_cleared:
                        generated = 0
                        storage_limit_notified = False
                        imported_offset = 0
                        imported_total = 0
                        if args.balance_mode == "local-node":
                            active_wallet_name = (
                                f"{args.watchonly_wallet_base_name}_{int(time.time())}"
                            )
                            try:
                                ensure_local_watchonly_wallet(
                                    args.bitcoin_cli_path, active_wallet_name
                                )
                                local_node_ready = True
                            except Exception as exc:  # pylint: disable=broad-except
                                local_node_ready = False
                                print(
                                    f"Could not create fresh watch-only wallet "
                                    f"({active_wallet_name}): {exc}"
                                )
                        print("Received /new command; backlog cleared.")
                except Exception as exc:  # pylint: disable=broad-except
                    print(f"Telegram polling error: {exc}")
                next_poll = now + max(1.0, args.telegram_poll_seconds)

            if (
                args.balance_mode == "local-node"
                and local_node_ready
                and now >= next_import_sync
            ):
                try:
                    new_addresses, imported_offset, was_truncated = (
                        read_new_addresses_from_backlog(
                            output_file=args.output,
                            start_offset=imported_offset,
                            max_records=max(1, args.import_batch_size),
                        )
                    )
                    if was_truncated:
                        imported_total = 0
                    if new_addresses:
                        imported_now = import_addresses_to_local_wallet(
                            bitcoin_cli_path=args.bitcoin_cli_path,
                            wallet_name=active_wallet_name,
                            addresses=new_addresses,
                        )
                        imported_total += imported_now
                        print(
                            f"Imported {imported_now}/{len(new_addresses)} address(es) "
                            f"to {active_wallet_name}."
                        )
                except Exception as exc:  # pylint: disable=broad-except
                    print(f"Local-node import sync error: {exc}")
                next_import_sync = now + max(0.2, args.import_sync_interval_seconds)

            should_send_scheduled_summary = telegram_enabled and now >= next_summary
            should_send_manual_summary = telegram_enabled and manual_summary_requested
            if should_send_scheduled_summary or should_send_manual_summary:
                try:
                    if args.balance_mode == "local-node":
                        if not local_node_ready:
                            raise RuntimeError(
                                "Local node mode enabled but wallet is unavailable."
                            )
                    summary_reason = (
                        "manual /check"
                        if should_send_manual_summary and not should_send_scheduled_summary
                        else "hourly"
                    )
                    send_balance_summary(
                        bot_token=args.telegram_bot_token,
                        chat_id=str(args.telegram_chat_id),
                        balance_mode=args.balance_mode,
                        bitcoin_cli_path=args.bitcoin_cli_path,
                        wallet_name=active_wallet_name,
                        output_file=args.output,
                        balance_api_base=args.balance_api_base,
                        reason=summary_reason,
                    )
                except Exception as exc:  # pylint: disable=broad-except
                    print(f"Hourly summary error: {exc}")
                    try:
                        send_telegram_message(
                            bot_token=args.telegram_bot_token,
                            chat_id=str(args.telegram_chat_id),
                            text=f"Balance summary failed: {exc}",
                        )
                    except Exception:  # pylint: disable=broad-except
                        pass
                if should_send_scheduled_summary:
                    next_summary = now + max(10.0, args.telegram_summary_interval_seconds)
                manual_summary_requested = False

            storage_bytes = backlog_size_bytes(args.output)
            if storage_bytes >= storage_limit_bytes:
                if telegram_enabled and not storage_limit_notified:
                    try:
                        send_telegram_message(
                            bot_token=args.telegram_bot_token,
                            chat_id=str(args.telegram_chat_id),
                            text=(
                                "Wallet generation paused: storage cap reached.\n"
                                f"Backlog size: {storage_bytes / (1024**3):.2f} GB "
                                f"(limit {args.storage_limit_gb:.2f} GB).\n"
                                "Send /new to clear backlog and continue."
                            ),
                        )
                    except Exception as exc:  # pylint: disable=broad-except
                        print(f"Storage warning send failed: {exc}")
                storage_limit_notified = True
            elif now >= next_generation:
                current_load = normalized_load()
                if args.ignore_load or current_load <= args.max_load_per_cpu:
                    wallet = generate_wallet(include_private_key=args.include_private_key)
                    write_jsonl(args.output, wallet)
                    generated += 1
                    storage_limit_notified = False
                    print(f"[{generated}] Generated address {wallet['address']}")
                next_generation = now + max(0.0, args.interval_seconds)

            save_state(
                args.state_file,
                {
                    "generated": generated,
                    "imported_offset": imported_offset,
                    "imported_total": imported_total,
                    "telegram_offset": telegram_offset,
                    "storage_limit_notified": storage_limit_notified,
                    "active_watchonly_wallet": active_wallet_name,
                    "local_node_ready": local_node_ready,
                    "updated_at_utc": datetime.now(tz=timezone.utc).isoformat(),
                },
            )
            time.sleep(0.2)
    except KeyboardInterrupt:
        print(f"\nStopped. Generated {generated} wallet(s).")


if __name__ == "__main__":
    main()
