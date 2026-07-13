#!/usr/bin/env python3
"""Continuously generate Bitcoin wallets with random 12-word seed phrases."""

from __future__ import annotations

import argparse
import json
import os
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


def iter_wallet_records(output_file: Path) -> list[dict[str, Any]]:
    if not output_file.exists():
        return []

    records: list[dict[str, Any]] = []
    with output_file.open("r", encoding="utf-8") as f:
        for line in f:
            raw_line = line.strip()
            if not raw_line:
                continue
            try:
                parsed = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                records.append(parsed)
    return records


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


def process_new_command(
    bot_token: str,
    expected_chat_id: str,
    updates_offset: int,
    output_file: Path,
) -> tuple[int, bool]:
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
        if text.split()[0].lower() != "/new":
            continue

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

    return last_seen_update, was_cleared


def fetch_balance_sats(balance_api_base: str, address: str) -> int:
    url = f"{balance_api_base.rstrip('/')}/address/{address}"
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
    return max(0, chain_balance + mempool_balance)


def compute_total_balance(
    output_file: Path, balance_api_base: str
) -> tuple[int, int, int]:
    records = iter_wallet_records(output_file)
    addresses = {
        str(record.get("address", "")).strip()
        for record in records
        if record.get("address")
    }
    addresses = {address for address in addresses if address}

    total_sats = 0
    failed_lookups = 0
    for address in sorted(addresses):
        try:
            total_sats += fetch_balance_sats(balance_api_base, address)
        except Exception:  # pylint: disable=broad-except
            failed_lookups += 1
        time.sleep(0.03)

    return len(addresses), total_sats, failed_lookups


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

    next_poll = time.monotonic()
    next_summary = time.monotonic() + max(10.0, args.telegram_summary_interval_seconds)
    next_generation = time.monotonic()

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

            if telegram_enabled and now >= next_poll:
                try:
                    telegram_offset, was_cleared = process_new_command(
                        bot_token=args.telegram_bot_token,
                        expected_chat_id=str(args.telegram_chat_id),
                        updates_offset=telegram_offset,
                        output_file=args.output,
                    )
                    if was_cleared:
                        generated = 0
                        storage_limit_notified = False
                        print("Received /new command; backlog cleared.")
                except Exception as exc:  # pylint: disable=broad-except
                    print(f"Telegram polling error: {exc}")
                next_poll = now + max(1.0, args.telegram_poll_seconds)

            if telegram_enabled and now >= next_summary:
                try:
                    wallet_count, total_sats, failed_lookups = compute_total_balance(
                        output_file=args.output,
                        balance_api_base=args.balance_api_base,
                    )
                    message = (
                        "Bitcoin wallet hourly summary\n"
                        f"Wallets tracked: {wallet_count}\n"
                        f"Total BTC: {total_sats / 100_000_000:.8f}\n"
                        f"Failed balance checks: {failed_lookups}"
                    )
                    send_telegram_message(
                        bot_token=args.telegram_bot_token,
                        chat_id=str(args.telegram_chat_id),
                        text=message,
                    )
                except Exception as exc:  # pylint: disable=broad-except
                    print(f"Hourly summary error: {exc}")
                next_summary = now + max(10.0, args.telegram_summary_interval_seconds)

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
                    "telegram_offset": telegram_offset,
                    "storage_limit_notified": storage_limit_notified,
                    "updated_at_utc": datetime.now(tz=timezone.utc).isoformat(),
                },
            )
            time.sleep(0.2)
    except KeyboardInterrupt:
        print(f"\nStopped. Generated {generated} wallet(s).")


if __name__ == "__main__":
    main()
