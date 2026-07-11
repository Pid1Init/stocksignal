#!/usr/bin/env python3
"""Weekly Hong Kong stock signal scanner with Telegram alerts."""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import pandas as pd
import requests
import yfinance as yf
from zoneinfo import ZoneInfo


HKT = ZoneInfo("Asia/Hong_Kong")

# The 46 watchlist symbols supplied by user (Hong Kong exchange codes).
WATCHLIST_CODES = [
    "700",
    "981",
    "1299",
    "1810",
    "388",
    "9633",
    "2388",
    "3690",
    "16",
    "2259",
    "1888",
    "992",
    "1",
    "669",
    "1109",
    "9961",
    "1378",
    "3692",
    "9992",
    "175",
    "1024",
    "9903",
    "2269",
    "1801",
    "27",
    "6082",
    "6990",
    "288",
    "148",
    "100",
    "823",
    "9868",
    "2015",
    "9926",
    "1093",
    "1519",
    "522",
    "1177",
    "2268",
    "9660",
    "2382",
    "2319",
    "20",
    "2099",
    "2577",
    "9880",
]


def to_yahoo_symbol(code: str) -> str:
    """Convert HK stock code into Yahoo Finance ticker format."""
    return f"{code.zfill(4)}.HK"


@dataclass
class SignalResult:
    symbol: str
    code: str
    signal_type: str
    prev_open: float
    prev_close: float
    prev_high: float
    prev_low: float
    curr_open: float
    curr_close: float
    midpoint: float
    pct_change_4w: float


def download_daily_ohlc(symbol: str, lookback_months: int = 6) -> pd.DataFrame:
    """Download daily OHLC from Yahoo Finance."""
    df = yf.download(
        symbol,
        period=f"{lookback_months}mo",
        interval="1d",
        auto_adjust=False,
        progress=False,
        threads=False,
    )
    if df.empty:
        return df

    # yfinance index is typically timezone-naive trading date. Treat it as
    # exchange date and convert into explicit HKT for stable weekly grouping.
    idx = pd.DatetimeIndex(df.index)
    if idx.tz is None:
        idx = idx.tz_localize(HKT)
    else:
        idx = idx.tz_convert(HKT)
    df.index = idx

    # yfinance can return either:
    # 1) flat columns: Open/High/Low/Close/Volume
    # 2) MultiIndex columns, e.g. (Price, Ticker) or (Ticker, Price)
    # Normalize into a flat OHLCV frame regardless of the format.
    if isinstance(df.columns, pd.MultiIndex):
        if symbol in df.columns.get_level_values(0):
            flattened = df.xs(symbol, axis=1, level=0)
        elif symbol in df.columns.get_level_values(1):
            flattened = df.xs(symbol, axis=1, level=1)
        else:
            # Fallback: flatten tuples and use first matching OHLCV labels.
            flattened = df.copy()
            flattened.columns = [
                "_".join(str(part) for part in col if part is not None)
                for col in flattened.columns.to_flat_index()
            ]
            rename_map = {}
            for col in flattened.columns:
                for field in ("Open", "High", "Low", "Close", "Volume"):
                    if col.startswith(f"{field}_") or col.endswith(f"_{field}"):
                        rename_map[col] = field
                        break
            flattened = flattened.rename(columns=rename_map)
        df = flattened

    required_cols = ["Open", "High", "Low", "Close", "Volume"]
    missing_cols = [col for col in required_cols if col not in df.columns]
    if missing_cols:
        raise KeyError(
            f"Missing OHLCV columns after normalization: {missing_cols}; "
            f"available={list(df.columns)}"
        )

    return df[required_cols].dropna()


def daily_to_weekly(df_daily: pd.DataFrame) -> pd.DataFrame:
    """
    Convert daily OHLCV to weekly candles.

    Week is aligned to Hong Kong trading weeks with Friday close labels.
    """
    if df_daily.empty:
        return df_daily

    weekly = df_daily.resample("W-FRI").agg(
        {
            "Open": "first",
            "High": "max",
            "Low": "min",
            "Close": "last",
            "Volume": "sum",
        }
    )
    return weekly.dropna()


def evaluate_signals(
    symbol: str,
    weekly: pd.DataFrame,
    bullish_threshold: float,
    bearish_threshold: float,
) -> List[SignalResult]:
    """Evaluate both 5-condition patterns on weekly candles."""
    if len(weekly) < 6:
        return []

    prev = weekly.iloc[-2]
    curr = weekly.iloc[-1]
    close_4w_ago = float(weekly.iloc[-5]["Close"])
    pct_change_4w = ((float(curr["Close"]) / close_4w_ago) - 1.0) * 100
    midpoint = (float(prev["Open"]) + float(prev["Close"])) / 2.0

    code = symbol.replace(".HK", "")
    results: List[SignalResult] = []

    # Signal set #2 (bullish setup)
    cond_2_1 = float(prev["Open"]) > float(prev["Close"])  # prior red
    cond_2_2 = float(curr["Open"]) < float(curr["Close"])  # current green
    cond_2_3 = float(curr["Open"]) < float(prev["Low"])
    cond_2_4 = float(curr["Close"]) > midpoint
    cond_2_5 = pct_change_4w > bullish_threshold

    if all([cond_2_1, cond_2_2, cond_2_3, cond_2_4, cond_2_5]):
        results.append(
            SignalResult(
                symbol=symbol,
                code=code,
                signal_type="Bullish (#2)",
                prev_open=float(prev["Open"]),
                prev_close=float(prev["Close"]),
                prev_high=float(prev["High"]),
                prev_low=float(prev["Low"]),
                curr_open=float(curr["Open"]),
                curr_close=float(curr["Close"]),
                midpoint=midpoint,
                pct_change_4w=pct_change_4w,
            )
        )

    # Signal set #3 (bearish setup)
    cond_3_1 = float(prev["Open"]) < float(prev["Close"])  # prior green
    cond_3_2 = float(curr["Open"]) > float(curr["Close"])  # current red
    cond_3_3 = float(curr["Open"]) > float(prev["High"])
    cond_3_4 = float(curr["Close"]) < midpoint
    cond_3_5 = pct_change_4w < bearish_threshold

    if all([cond_3_1, cond_3_2, cond_3_3, cond_3_4, cond_3_5]):
        results.append(
            SignalResult(
                symbol=symbol,
                code=code,
                signal_type="Bearish (#3)",
                prev_open=float(prev["Open"]),
                prev_close=float(prev["Close"]),
                prev_high=float(prev["High"]),
                prev_low=float(prev["Low"]),
                curr_open=float(curr["Open"]),
                curr_close=float(curr["Close"]),
                midpoint=midpoint,
                pct_change_4w=pct_change_4w,
            )
        )

    return results


def format_alert(signals: List[SignalResult]) -> str:
    """Build Telegram message text."""
    now_hkt = datetime.now(HKT).strftime("%Y-%m-%d %H:%M:%S %Z")
    lines = [
        "HK Weekly OHLC Signal Scan",
        f"Run time: {now_hkt}",
        f"Signals found: {len(signals)}",
        "",
    ]
    for s in signals:
        lines.extend(
            [
                f"{s.code} ({s.symbol}) - {s.signal_type}",
                (
                    f"Prev O/C/H/L: {s.prev_open:.2f}/{s.prev_close:.2f}/"
                    f"{s.prev_high:.2f}/{s.prev_low:.2f}"
                ),
                (
                    f"Curr O/C: {s.curr_open:.2f}/{s.curr_close:.2f}, "
                    f"Midpoint(prev O/C): {s.midpoint:.2f}"
                ),
                f"4-week price change: {s.pct_change_4w:.2f}%",
                "",
            ]
        )
    return "\n".join(lines).strip()


def send_telegram_message(bot_token: str, chat_id: str, text: str) -> None:
    """Send message using Telegram Bot API."""
    api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    response = requests.post(
        api_url,
        data={
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": "true",
        },
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok", False):
        raise RuntimeError(f"Telegram API returned error: {payload}")


def run_scan(
    symbols: List[str],
    bullish_threshold: float,
    bearish_threshold: float,
) -> Tuple[List[SignalResult], Dict[str, str]]:
    """Run scan over all symbols. Returns signals and per-symbol errors."""
    signals: List[SignalResult] = []
    errors: Dict[str, str] = {}

    for symbol in symbols:
        try:
            daily = download_daily_ohlc(symbol)
            weekly = daily_to_weekly(daily)
            signals.extend(
                evaluate_signals(
                    symbol=symbol,
                    weekly=weekly,
                    bullish_threshold=bullish_threshold,
                    bearish_threshold=bearish_threshold,
                )
            )
        except Exception as exc:  # pylint: disable=broad-except
            errors[symbol] = str(exc)
    return signals, errors


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scan HK watchlist weekly signals and alert via Telegram."
    )
    parser.add_argument(
        "--bullish-threshold",
        type=float,
        default=5.0,
        help="Criterion 2.5 threshold in percent (default: 5.0).",
    )
    parser.add_argument(
        "--bearish-threshold",
        type=float,
        default=-5.0,
        help=(
            "Criterion 3.5 threshold in percent. "
            "Default is -5.0 for a 5%% drop vs 4 weeks ago."
        ),
    )
    parser.add_argument(
        "--always-send",
        action="store_true",
        help="Send Telegram message even if no signal is found.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    if not bot_token or not chat_id:
        print(
            "Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID environment variable.",
            file=sys.stderr,
        )
        return 2

    symbols = [to_yahoo_symbol(code) for code in WATCHLIST_CODES]
    signals, errors = run_scan(
        symbols=symbols,
        bullish_threshold=args.bullish_threshold,
        bearish_threshold=args.bearish_threshold,
    )

    should_send = bool(signals) or args.always_send
    if should_send:
        message = (
            format_alert(signals)
            if signals
            else "HK Weekly OHLC Signal Scan\nNo matching signals this week."
        )
        if errors:
            error_preview = "\n".join(
                f"- {sym}: {msg}" for sym, msg in list(errors.items())[:10]
            )
            message = (
                f"{message}\n\nData errors on {len(errors)} symbol(s):\n{error_preview}"
            )
        send_telegram_message(bot_token, chat_id, message)

    print(f"Scan complete. Signals: {len(signals)}. Errors: {len(errors)}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
