#!/usr/bin/env python3
"""Check balances for owned Bitcoin addresses using a local Bitcoin Core node."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from decimal import Decimal
from pathlib import Path
from typing import Any


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


def ensure_watchonly_wallet(bitcoin_cli_path: str, wallet_name: str) -> None:
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
    payload = run_bitcoin_cli(bitcoin_cli_path, ["getdescriptorinfo", f"addr({address})"])
    if not isinstance(payload, dict):
        raise RuntimeError(f"Unexpected getdescriptorinfo payload for address {address}")
    descriptor = str(payload.get("descriptor", "")).strip()
    if not descriptor:
        raise RuntimeError(f"Missing descriptor for address {address}")
    return descriptor


def import_addresses(
    bitcoin_cli_path: str,
    wallet_name: str,
    addresses: list[str],
    rescan: bool,
) -> int:
    if not addresses:
        return 0

    payload = []
    for address in addresses:
        payload.append(
            {
                "desc": descriptor_for_address(bitcoin_cli_path, address),
                "timestamp": 0 if rescan else "now",
                "active": False,
                "internal": False,
                "label": "owned-address-check",
            }
        )

    results = run_bitcoin_cli(
        bitcoin_cli_path,
        ["importdescriptors", json.dumps(payload, ensure_ascii=True)],
        wallet_name=wallet_name,
    )
    if not isinstance(results, list):
        return 0
    imported = 0
    for item in results:
        if isinstance(item, dict) and item.get("success", False):
            imported += 1
    return imported


def parse_addresses(addresses_file: Path) -> list[str]:
    addresses: list[str] = []
    seen: set[str] = set()
    with addresses_file.open("r", encoding="utf-8") as f:
        for line in f:
            raw = line.strip()
            if not raw:
                continue

            address = ""
            try:
                payload = json.loads(raw)
                if isinstance(payload, dict):
                    address = str(payload.get("address", "")).strip()
                elif isinstance(payload, str):
                    address = payload.strip()
            except json.JSONDecodeError:
                address = raw

            if not address or address in seen:
                continue
            seen.add(address)
            addresses.append(address)
    return addresses


def chunked(items: list[str], batch_size: int) -> list[list[str]]:
    return [items[i : i + batch_size] for i in range(0, len(items), batch_size)]


def gather_positive_balances(bitcoin_cli_path: str, wallet_name: str) -> dict[str, int]:
    unspent = run_bitcoin_cli(
        bitcoin_cli_path,
        [
            "listunspent",
            "0",
            "9999999",
            "[]",
            "true",
            json.dumps({"minimumAmount": "0.00000001"}, ensure_ascii=True),
        ],
        wallet_name=wallet_name,
    )
    if not isinstance(unspent, list):
        return {}

    balances_sats: dict[str, int] = {}
    for utxo in unspent:
        if not isinstance(utxo, dict):
            continue
        address = str(utxo.get("address", "")).strip()
        if not address:
            continue
        amount_btc = Decimal(str(utxo.get("amount", "0")))
        sats = int(amount_btc * Decimal("100000000"))
        if sats <= 0:
            continue
        balances_sats[address] = balances_sats.get(address, 0) + sats
    return balances_sats


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read owned Bitcoin addresses from file, check balances with local "
            "Bitcoin Core, and output only positive-balance addresses."
        )
    )
    parser.add_argument(
        "--addresses-file",
        type=Path,
        required=True,
        help="Path to owned addresses file (JSONL with `address` or one address per line).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("./positive_balance_wallets.jsonl"),
        help="Output JSONL path for positive-balance results.",
    )
    parser.add_argument(
        "--wallet-name",
        default="owned_balance_checker",
        help="Descriptor watch-only wallet name used for imports.",
    )
    parser.add_argument(
        "--bitcoin-cli-path",
        default="/usr/local/bin/bitcoin-cli",
        help="Path to bitcoin-cli.",
    )
    parser.add_argument(
        "--import-batch-size",
        type=int,
        default=2000,
        help="Number of addresses imported per batch.",
    )
    parser.add_argument(
        "--rescan",
        action="store_true",
        help="Use full historical rescan (slow; may fail on pruned nodes).",
    )
    parser.add_argument(
        "--pause-ms-between-batches",
        type=int,
        default=0,
        help="Optional pause between import batches.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.import_batch_size <= 0:
        raise SystemExit("--import-batch-size must be positive.")
    if not args.addresses_file.exists():
        raise SystemExit(f"Addresses file not found: {args.addresses_file}")

    addresses = parse_addresses(args.addresses_file)
    if not addresses:
        raise SystemExit("No addresses found in input file.")
    print(f"Loaded {len(addresses)} unique address(es) from {args.addresses_file}")

    ensure_watchonly_wallet(args.bitcoin_cli_path, args.wallet_name)
    batches = chunked(addresses, args.import_batch_size)
    imported_total = 0
    for idx, batch in enumerate(batches, start=1):
        imported = import_addresses(
            bitcoin_cli_path=args.bitcoin_cli_path,
            wallet_name=args.wallet_name,
            addresses=batch,
            rescan=args.rescan,
        )
        imported_total += imported
        print(f"Imported batch {idx}/{len(batches)}: {imported}/{len(batch)}")
        if args.pause_ms_between_batches > 0:
            time.sleep(args.pause_ms_between_batches / 1000.0)

    print(f"Import complete: {imported_total}/{len(addresses)} addresses imported.")

    balances_sats = gather_positive_balances(args.bitcoin_cli_path, args.wallet_name)
    positive = []
    for address in addresses:
        sats = balances_sats.get(address, 0)
        if sats > 0:
            positive.append(
                {
                    "address": address,
                    "balance_sats": sats,
                    "balance_btc": f"{Decimal(sats) / Decimal('100000000'):.8f}",
                }
            )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as f:
        for item in positive:
            f.write(json.dumps(item, ensure_ascii=True) + "\n")

    print(
        f"Found {len(positive)} positive-balance address(es). "
        f"Output written to {args.output.resolve()}"
    )


if __name__ == "__main__":
    main()
