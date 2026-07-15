#!/usr/bin/env python3
"""Build a local SQLite balance index from a UTXO/address snapshot dump."""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import time
from pathlib import Path
from typing import Iterable


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build SQLite index (address -> balance_sats) from a UTXO/address dump."
        )
    )
    parser.add_argument(
        "--input",
        type=Path,
        required=True,
        help="Input dump path (CSV or JSONL).",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=Path("./utxo_balance_index.sqlite3"),
        help="Output SQLite database path.",
    )
    parser.add_argument(
        "--format",
        choices=("csv", "jsonl"),
        required=True,
        help="Input file format.",
    )
    parser.add_argument(
        "--address-field",
        default="address",
        help="Address field/column name in input.",
    )
    parser.add_argument(
        "--balance-field",
        default="balance_sats",
        help="Balance field/column name in input (sats).",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=20_000,
        help="Rows per transaction batch.",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=200_000,
        help="Progress print interval.",
    )
    return parser.parse_args()


def rows_from_csv(
    path: Path, address_field: str, balance_field: str
) -> Iterable[tuple[str, int]]:
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            address = str(row.get(address_field, "")).strip()
            balance_raw = str(row.get(balance_field, "")).strip()
            if not address or not balance_raw:
                continue
            try:
                sats = int(balance_raw)
            except ValueError:
                continue
            if sats <= 0:
                continue
            yield address, sats


def rows_from_jsonl(
    path: Path, address_field: str, balance_field: str
) -> Iterable[tuple[str, int]]:
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            raw = line.strip()
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            address = str(payload.get(address_field, "")).strip()
            balance_raw = payload.get(balance_field, None)
            if not address or balance_raw is None:
                continue
            try:
                sats = int(balance_raw)
            except (ValueError, TypeError):
                continue
            if sats <= 0:
                continue
            yield address, sats


def main() -> None:
    args = parse_args()
    if args.batch_size <= 0:
        raise SystemExit("--batch-size must be positive.")
    if args.progress_every <= 0:
        raise SystemExit("--progress-every must be positive.")
    if not args.input.exists():
        raise SystemExit(f"Input file not found: {args.input}")

    args.db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(args.db)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA temp_store=MEMORY;")
    conn.execute("PRAGMA cache_size=-200000;")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS utxo_balances (
            address TEXT PRIMARY KEY,
            balance_sats INTEGER NOT NULL CHECK(balance_sats >= 0)
        );
        """
    )
    conn.execute("DELETE FROM utxo_balances;")

    if args.format == "csv":
        source = rows_from_csv(args.input, args.address_field, args.balance_field)
    else:
        source = rows_from_jsonl(args.input, args.address_field, args.balance_field)

    started = time.monotonic()
    count = 0
    batch: list[tuple[str, int]] = []
    for address, sats in source:
        batch.append((address, sats))
        count += 1
        if len(batch) >= args.batch_size:
            conn.executemany(
                """
                INSERT INTO utxo_balances(address, balance_sats)
                VALUES(?, ?)
                ON CONFLICT(address) DO UPDATE SET
                    balance_sats = excluded.balance_sats;
                """,
                batch,
            )
            conn.commit()
            batch.clear()

        if count % args.progress_every == 0:
            elapsed = max(time.monotonic() - started, 0.001)
            print(f"Indexed {count} rows ({count / elapsed:.2f} rows/s)")

    if batch:
        conn.executemany(
            """
            INSERT INTO utxo_balances(address, balance_sats)
            VALUES(?, ?)
            ON CONFLICT(address) DO UPDATE SET
                balance_sats = excluded.balance_sats;
            """,
            batch,
        )
        conn.commit()

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_utxo_balances_sats ON utxo_balances(balance_sats);"
    )
    conn.commit()
    conn.close()

    elapsed = max(time.monotonic() - started, 0.001)
    print(
        f"Done. Indexed {count} rows into {args.db.resolve()} "
        f"in {elapsed:.1f}s ({count / elapsed:.2f} rows/s)."
    )


if __name__ == "__main__":
    main()
