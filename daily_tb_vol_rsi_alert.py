#!/usr/bin/env python3
"""Daily TB_VOL_RSI_V1 scanner with Telegram alerts."""

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
STRATEGY_ID = "TB_VOL_RSI_V1"

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
    return f"{code.zfill(4)}.HK"


def normalize_ohlcv(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """Normalize yfinance data into flat OHLCV columns."""
    if isinstance(df.columns, pd.MultiIndex):
        if symbol in df.columns.get_level_values(0):
            flattened = df.xs(symbol, axis=1, level=0)
        elif symbol in df.columns.get_level_values(1):
            flattened = df.xs(symbol, axis=1, level=1)
        else:
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

    required = ["Open", "High", "Low", "Close", "Volume"]
    missing = [col for col in required if col not in df.columns]
    if missing:
        raise KeyError(
            f"Missing OHLCV columns after normalization: {missing}; "
            f"available={list(df.columns)}"
        )
    return df[required].dropna()


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

    idx = pd.DatetimeIndex(df.index)
    if idx.tz is None:
        idx = idx.tz_localize(HKT)
    else:
        idx = idx.tz_convert(HKT)
    df.index = idx
    return normalize_ohlcv(df, symbol)


def compute_rsi14(close: pd.Series) -> pd.Series:
    """Compute RSI(14) with Wilder smoothing."""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)

    avg_gain = gain.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    avg_loss = loss.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()

    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    rsi = rsi.where(avg_loss != 0, 100.0)
    return rsi


@dataclass
class DailySignal:
    symbol: str
    code: str
    signal_date: str
    signal_open: float
    signal_high: float
    signal_low: float
    signal_close: float
    prior_low: float
    low_diff_pct: float
    volume_ratio: float
    rsi14: float


def detect_latest_signal(df: pd.DataFrame, symbol: str) -> Optional[DailySignal]:
    """Check signal only on the latest available trading day."""
    if len(df) < 60:
        return None

    data = df.copy()
    data["SMA20_VOL"] = data["Volume"].rolling(20).mean()
    data["RSI14"] = compute_rsi14(data["Close"])

    prev = data.iloc[-2]
    curr = data.iloc[-1]

    c1_is_red = float(prev["Close"]) < float(prev["Open"])
    c2_is_green = float(curr["Close"]) > float(curr["Open"])
    low_diff_pct = abs(float(prev["Low"]) - float(curr["Low"])) / float(curr["Close"]) * 100
    lows_match = low_diff_pct <= 0.5

    sma20_vol = float(curr["SMA20_VOL"])
    if sma20_vol <= 0 or pd.isna(sma20_vol):
        return None

    volume_ratio = float(curr["Volume"]) / sma20_vol
    volume_ok = volume_ratio >= 1.5

    rsi14 = float(curr["RSI14"]) if not pd.isna(curr["RSI14"]) else float("nan")
    rsi_ok = not pd.isna(rsi14) and 60 <= rsi14 <= 80

    if not (c1_is_red and c2_is_green and lows_match and volume_ok and rsi_ok):
        return None

    date_str = pd.Timestamp(data.index[-1]).tz_convert(HKT).strftime("%Y-%m-%d")
    return DailySignal(
        symbol=symbol,
        code=symbol.replace(".HK", ""),
        signal_date=date_str,
        signal_open=float(curr["Open"]),
        signal_high=float(curr["High"]),
        signal_low=float(curr["Low"]),
        signal_close=float(curr["Close"]),
        prior_low=float(prev["Low"]),
        low_diff_pct=low_diff_pct,
        volume_ratio=volume_ratio,
        rsi14=rsi14,
    )


def format_signal_message(signals: List[DailySignal]) -> str:
    now_hkt = datetime.now(HKT).strftime("%Y-%m-%d %H:%M:%S %Z")
    lines = [
        f"{STRATEGY_ID} Daily Scan",
        f"Run time: {now_hkt}",
        f"Signals found: {len(signals)}",
        "",
    ]
    for s in signals:
        lines.extend(
            [
                f"{s.code} ({s.symbol}) | Date: {s.signal_date}",
                "Pattern: Tweezer Bottom (C1 red, C2 green, matching lows <= 0.5%)",
                (
                    f"Signal OHLC: {s.signal_open:.2f} / {s.signal_high:.2f} / "
                    f"{s.signal_low:.2f} / {s.signal_close:.2f}"
                ),
                f"Prior low: {s.prior_low:.2f}, low diff: {s.low_diff_pct:.3f}%",
                f"Volume ratio (vs SMA20): {s.volume_ratio:.2f}",
                f"RSI14: {s.rsi14:.2f} (required 60-80)",
                "Trade plan: Entry next trading day open, TP +35%, SL -7%, Time stop 20 days",
                "",
            ]
        )
    return "\n".join(lines).strip()


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


def scan_all_symbols(symbols: List[str]) -> Tuple[List[DailySignal], Dict[str, str]]:
    signals: List[DailySignal] = []
    errors: Dict[str, str] = {}
    for symbol in symbols:
        try:
            daily = download_daily_ohlc(symbol)
            if daily.empty:
                continue
            signal = detect_latest_signal(daily, symbol)
            if signal:
                signals.append(signal)
        except Exception as exc:  # pylint: disable=broad-except
            errors[symbol] = str(exc)
    return signals, errors


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run TB_VOL_RSI_V1 daily scan and send Telegram results."
    )
    parser.add_argument(
        "--only-on-signal",
        action="store_true",
        help="Send Telegram only when at least one signal is found.",
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
    signals, errors = scan_all_symbols(symbols)

    should_send = bool(signals) or not args.only_on_signal
    if should_send:
        message = (
            format_signal_message(signals)
            if signals
            else f"{STRATEGY_ID} Daily Scan\nNo matching signals today."
        )
        if errors:
            error_preview = "\n".join(
                f"- {sym}: {msg}" for sym, msg in list(errors.items())[:10]
            )
            message = (
                f"{message}\n\nData errors on {len(errors)} symbol(s):\n{error_preview}"
            )
        send_telegram_message(bot_token, chat_id, message)

    print(f"{STRATEGY_ID} scan complete. Signals: {len(signals)}. Errors: {len(errors)}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
