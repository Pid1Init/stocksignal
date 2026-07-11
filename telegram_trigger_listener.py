#!/usr/bin/env python3
"""Telegram /trigger listener for running all repository signal scans."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from typing import Dict, List, Optional

import requests
from zoneinfo import ZoneInfo

from daily_tb_vol_rsi_alert import (
    STRATEGY_ID as DAILY_STRATEGY_ID,
    format_signal_message as format_daily_message,
    scan_all_symbols as scan_daily_symbols,
    to_yahoo_symbol as to_daily_symbol,
    WATCHLIST_CODES as DAILY_WATCHLIST_CODES,
)
from weekly_hk_stock_alert import (
    format_alert as format_weekly_message,
    run_scan as run_weekly_scan,
    to_yahoo_symbol as to_weekly_symbol,
    WATCHLIST_CODES as WEEKLY_WATCHLIST_CODES,
)


HKT = ZoneInfo("Asia/Hong_Kong")
OFFSET_FILE_DEFAULT = ".telegram_update_offset"


def get_updates(bot_token: str, offset: Optional[int], timeout: int) -> List[dict]:
    params = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset
    url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
    response = requests.get(url, params=params, timeout=timeout + 10)
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok", False):
        raise RuntimeError(f"Telegram getUpdates failed: {payload}")
    return payload.get("result", [])


def send_message(bot_token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    response = requests.post(
        url,
        data={"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok", False):
        raise RuntimeError(f"Telegram sendMessage failed: {payload}")


def load_offset(path: str) -> Optional[int]:
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as fp:
        raw = fp.read().strip()
    if not raw:
        return None
    return int(raw)


def save_offset(path: str, update_id: int) -> None:
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as fp:
        fp.write(str(update_id + 1))
    os.replace(tmp_path, path)


def build_trigger_response() -> str:
    now_hkt = datetime.now(HKT).strftime("%Y-%m-%d %H:%M:%S %Z")

    weekly_symbols = [to_weekly_symbol(code) for code in WEEKLY_WATCHLIST_CODES]
    weekly_signals, weekly_errors = run_weekly_scan(
        symbols=weekly_symbols,
        bullish_threshold=5.0,
        bearish_threshold=-5.0,
    )

    daily_symbols = [to_daily_symbol(code) for code in DAILY_WATCHLIST_CODES]
    daily_signals, daily_errors = scan_daily_symbols(daily_symbols)

    sections: List[str] = [f"Manual trigger finished at {now_hkt}", ""]

    weekly_text = (
        format_weekly_message(weekly_signals)
        if weekly_signals
        else "HK Weekly OHLC Signal Scan\nNo matching signals this week."
    )
    sections.append(weekly_text)
    if weekly_errors:
        preview = "\n".join(
            f"- {sym}: {msg}" for sym, msg in list(weekly_errors.items())[:10]
        )
        sections.append(
            f"Weekly data errors on {len(weekly_errors)} symbol(s):\n{preview}"
        )

    sections.extend(["", "=" * 30, ""])

    daily_text = (
        format_daily_message(daily_signals)
        if daily_signals
        else f"{DAILY_STRATEGY_ID} Daily Scan\nNo matching signals today."
    )
    sections.append(daily_text)
    if daily_errors:
        preview = "\n".join(
            f"- {sym}: {msg}" for sym, msg in list(daily_errors.items())[:10]
        )
        sections.append(f"Daily data errors on {len(daily_errors)} symbol(s):\n{preview}")

    return "\n".join(sections).strip()


def is_trigger_command(text: str) -> bool:
    if not text:
        return False
    command = text.strip().split()[0].lower()
    return command == "/trigger" or command.startswith("/trigger@")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Process Telegram /trigger command and run all scans."
    )
    parser.add_argument(
        "--offset-file",
        default=OFFSET_FILE_DEFAULT,
        help=f"Path to store Telegram update offset (default: {OFFSET_FILE_DEFAULT}).",
    )
    parser.add_argument(
        "--poll-timeout",
        type=int,
        default=0,
        help="Telegram getUpdates timeout seconds (default: 0 for one-shot polling).",
    )
    parser.add_argument(
        "--ignore-old-updates",
        action="store_true",
        help="Advance offset to the latest update without processing older backlog.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    allowed_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not bot_token or not allowed_chat_id:
        print(
            "Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID environment variable.",
            file=sys.stderr,
        )
        return 2

    offset = load_offset(args.offset_file)
    updates = get_updates(bot_token, offset=offset, timeout=max(0, args.poll_timeout))
    if not updates:
        return 0

    if args.ignore_old_updates:
        save_offset(args.offset_file, int(updates[-1]["update_id"]))
        return 0

    for update in updates:
        update_id = int(update["update_id"])
        message = update.get("message") or update.get("edited_message")
        if not message:
            save_offset(args.offset_file, update_id)
            continue

        chat = message.get("chat", {})
        chat_id = str(chat.get("id", "")).strip()
        text = (message.get("text") or "").strip()

        if chat_id != allowed_chat_id:
            save_offset(args.offset_file, update_id)
            continue

        if is_trigger_command(text):
            try:
                response_text = build_trigger_response()
            except Exception as exc:  # pylint: disable=broad-except
                response_text = f"/trigger failed: {exc}"
            send_message(bot_token, chat_id, response_text)
        elif text.startswith("/"):
            send_message(
                bot_token,
                chat_id,
                "Supported command: /trigger",
            )

        save_offset(args.offset_file, update_id)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
