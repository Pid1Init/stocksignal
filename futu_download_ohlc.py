#!/usr/bin/env python3
"""Download daily OHLC data for 46 HK stocks from Futu OpenD."""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime
from typing import Dict, List, Tuple

import pandas as pd
from futu import (
    AuType,
    KLType,
    KL_FIELD,
    OpenQuoteContext,
    RET_OK,
)


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


def to_futu_symbol(code: str) -> str:
    """Convert numeric code to Futu symbol format."""
    return f"HK.{code.zfill(5)}"


def fetch_history_for_symbol(
    quote_ctx: OpenQuoteContext,
    symbol: str,
    start_date: str,
    end_date: str,
    max_retries: int,
) -> pd.DataFrame:
    """Fetch full daily kline history for one symbol with pagination."""
    fields = [
        KL_FIELD.DATE_TIME,
        KL_FIELD.OPEN,
        KL_FIELD.HIGH,
        KL_FIELD.LOW,
        KL_FIELD.CLOSE,
        KL_FIELD.VOL,
    ]
    req_key = None
    chunks: List[pd.DataFrame] = []

    while True:
        attempt = 0
        while True:
            ret, data, next_key = quote_ctx.request_history_kline(
                code=symbol,
                start=start_date,
                end=end_date,
                ktype=KLType.K_DAY,
                autype=AuType.QFQ,
                fields=fields,
                max_count=1000,
                page_req_key=req_key,
            )
            if ret == RET_OK:
                chunks.append(data)
                req_key = next_key
                break

            attempt += 1
            if attempt > max_retries:
                raise RuntimeError(f"{symbol}: {data}")
            time.sleep(min(2**attempt, 8))

        if req_key is None:
            break

    if not chunks:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])

    merged = pd.concat(chunks, ignore_index=True)
    merged = merged.rename(
        columns={
            "time_key": "date",
            "open": "open",
            "high": "high",
            "low": "low",
            "close": "close",
            "volume": "volume",
            "code": "symbol",
        }
    )
    keep_cols = ["symbol", "date", "open", "high", "low", "close", "volume"]
    return merged[keep_cols].sort_values("date").reset_index(drop=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download daily OHLC data from Futu for the 46 HK watchlist stocks."
    )
    parser.add_argument("--host", default="127.0.0.1", help="OpenD host")
    parser.add_argument("--port", type=int, default=11111, help="OpenD port")
    parser.add_argument(
        "--start-date",
        default="2021-01-01",
        help="Start date in YYYY-MM-DD (default: 2021-01-01)",
    )
    parser.add_argument(
        "--end-date",
        default=datetime.now().strftime("%Y-%m-%d"),
        help="End date in YYYY-MM-DD (default: today)",
    )
    parser.add_argument(
        "--output-dir",
        default="futu_ohlc",
        help="Output directory for CSV files",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=3,
        help="Max retries per request page",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    os.makedirs(args.output_dir, exist_ok=True)

    symbols = [to_futu_symbol(code) for code in WATCHLIST_CODES]
    all_rows: List[pd.DataFrame] = []
    errors: Dict[str, str] = {}

    quote_ctx = OpenQuoteContext(host=args.host, port=args.port)
    try:
        for symbol in symbols:
            try:
                df = fetch_history_for_symbol(
                    quote_ctx=quote_ctx,
                    symbol=symbol,
                    start_date=args.start_date,
                    end_date=args.end_date,
                    max_retries=args.max_retries,
                )
                all_rows.append(df)

                per_symbol_path = os.path.join(
                    args.output_dir, f"{symbol.replace('.', '_')}_daily.csv"
                )
                df.to_csv(per_symbol_path, index=False)
                print(f"Saved {symbol}: {len(df)} rows -> {per_symbol_path}")
            except Exception as exc:  # pylint: disable=broad-except
                errors[symbol] = str(exc)
                print(f"Failed {symbol}: {exc}", file=sys.stderr)
    finally:
        quote_ctx.close()

    if all_rows:
        merged = pd.concat(all_rows, ignore_index=True)
        merged_path = os.path.join(args.output_dir, "all_46_stocks_daily_ohlc.csv")
        merged.to_csv(merged_path, index=False)
        print(f"Saved merged file: {merged_path} ({len(merged)} rows)")

    if errors:
        error_path = os.path.join(args.output_dir, "download_errors.csv")
        pd.DataFrame(
            [{"symbol": symbol, "error": message} for symbol, message in errors.items()]
        ).to_csv(error_path, index=False)
        print(f"Completed with {len(errors)} error(s). See {error_path}.")
        return 1

    print("Completed successfully for all symbols.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
