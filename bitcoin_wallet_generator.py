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
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    generated = 0

    print(
        "Starting wallet generation. Press Ctrl+C to stop. "
        f"Output file: {args.output.resolve()}"
    )

    try:
        while True:
            if args.count > 0 and generated >= args.count:
                print(f"Reached requested count ({args.count}). Exiting.")
                break

            current_load = normalized_load()
            if not args.ignore_load and current_load > args.max_load_per_cpu:
                print(
                    "Skipping generation due to load "
                    f"({current_load:.2f} > {args.max_load_per_cpu:.2f})."
                )
                time.sleep(max(args.poll_seconds, 0.0))
                continue

            wallet = generate_wallet(include_private_key=args.include_private_key)
            write_jsonl(args.output, wallet)
            generated += 1
            print(
                f"[{generated}] Generated address {wallet['address']} "
                f"(mnemonic stored in {args.output})."
            )

            if args.interval_seconds > 0:
                time.sleep(args.interval_seconds)
    except KeyboardInterrupt:
        print(f"\nStopped. Generated {generated} wallet(s).")


if __name__ == "__main__":
    main()
