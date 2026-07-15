#!/usr/bin/env python3
"""Generate many Bitcoin wallets with random 12-word mnemonics."""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate Bitcoin wallets from random 12-word recovery seedphrases "
            "and store seedphrase-address pairs."
        )
    )
    parser.add_argument(
        "--count",
        type=int,
        default=1_000_000,
        help="Total number of wallets to generate (default: 1000000).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("./wallet_seed_address_pairs.jsonl"),
        help="JSONL output file path.",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=10_000,
        help="Print progress every N generated wallets (default: 10000).",
    )
    return parser.parse_args()


def generate_pair() -> dict[str, str]:
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
    return {
        "mnemonic": str(mnemonic),
        "address": account.PublicKey().ToAddress(),
        "created_at_utc": datetime.now(tz=timezone.utc).isoformat(),
        "derivation_path": "m/44'/0'/0'/0/0",
    }


def main() -> None:
    args = parse_args()
    if args.count <= 0:
        raise SystemExit("--count must be a positive integer.")
    if args.progress_every <= 0:
        raise SystemExit("--progress-every must be a positive integer.")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    print(f"Generating {args.count} wallet(s) into {args.output.resolve()}")

    with args.output.open("a", encoding="utf-8") as f:
        for idx in range(1, args.count + 1):
            payload = generate_pair()
            f.write(json.dumps(payload, ensure_ascii=True) + "\n")

            if idx % args.progress_every == 0 or idx == args.count:
                elapsed = max(time.monotonic() - started, 0.001)
                rate = idx / elapsed
                print(
                    f"Generated {idx}/{args.count} "
                    f"({rate:.2f} wallets/sec, elapsed {elapsed:.1f}s)"
                )

    elapsed = max(time.monotonic() - started, 0.001)
    print(
        f"Done. Generated {args.count} wallets in {elapsed:.1f}s "
        f"({args.count / elapsed:.2f} wallets/sec)."
    )


if __name__ == "__main__":
    main()
