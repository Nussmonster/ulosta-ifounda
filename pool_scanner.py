#!/usr/bin/env python3
"""
Dead/abandoned DEX pool scanner for Voi (or any AVM chain).

Read-only: it only queries a public indexer. Nothing is signed or sent.

    pip install "py-algorand-sdk>=2.0"
    python pool_scanner.py                          # scan KNOWN_POOLS
    python pool_scanner.py 47166025 47424655        # scan specific app IDs
    python pool_scanner.py --days 180 --json out.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone

from algosdk.error import IndexerHTTPError
from algosdk.logic import get_application_address
from algosdk.v2client import indexer

# --- Defaults (override with CLI flags) ---
INDEXER_URL = "https://mainnet-idx.voi.nodely.dev"
INDEXER_TOKEN = ""  # empty for the public Nodely free tier

# Placeholder app IDs -- replace with real pool app IDs from an explorer / pool registry.
KNOWN_POOLS = [
    47166025,
    47424655,
    47165570,
]

INACTIVE_DAYS = 90       # flag if no app calls in this many days
MIN_WHOLE_UNITS = 1.0    # ignore holdings below this many whole tokens (decimals-aware)
NATIVE_DECIMALS = 6      # VOI / ALGO
REQUEST_DELAY = 0.3      # seconds between pools, to be polite to the free indexer
MAX_RETRIES = 4


def is_not_found(err: Exception) -> bool:
    # py-algorand-sdk drops the HTTP status and keeps only the message, so match on text.
    # Indexer says e.g. "no application found for application-id: N" / "no accounts found for address: X".
    msg = str(err).lower()
    return "not found" in msg or ("no " in msg and " found" in msg)


def call(fn, *args, **kwargs):
    """Call an indexer method, retrying rate limits / transient errors (not 'not found')."""
    for attempt in range(MAX_RETRIES + 1):
        try:
            return fn(*args, **kwargs)
        except IndexerHTTPError as e:
            if is_not_found(e) or attempt == MAX_RETRIES:
                raise
        except OSError:  # network errors, timeouts
            if attempt == MAX_RETRIES:
                raise
        time.sleep(2 ** attempt)


class Scanner:
    def __init__(self, client: indexer.IndexerClient, inactive_days: int, min_units: float):
        self.client = client
        self.inactive_days = inactive_days
        self.min_units = min_units
        self._asset_cache: dict[int, dict] = {}

    def asset_meta(self, asset_id: int) -> dict:
        if asset_id == 0:
            return {"decimals": NATIVE_DECIMALS, "unit": "VOI"}
        if asset_id not in self._asset_cache:
            try:
                params = call(self.client.asset_info, asset_id)["asset"]["params"]
                self._asset_cache[asset_id] = {
                    "decimals": int(params.get("decimals", 0)),
                    "unit": params.get("unit-name") or params.get("name") or f"ASA{asset_id}",
                }
            except Exception:
                # Deleted/unknown asset: treat as 0 decimals so nothing is hidden.
                self._asset_cache[asset_id] = {"decimals": 0, "unit": f"ASA{asset_id}"}
        return self._asset_cache[asset_id]

    def check_pool(self, app_id: int) -> dict | None:
        escrow = get_application_address(app_id)
        print(f"\n=== Pool App {app_id} | Escrow {escrow} ===")

        # 1) Does the app exist?
        try:
            app = call(self.client.applications, app_id, include_all=True)["application"]
        except Exception as e:
            if is_not_found(e):
                print("  App not found on this network - skip")
                return None
            raise
        if app.get("deleted"):
            print("  App is deleted - skip")
            return None

        # 2) Balances held by the application account
        try:
            acct = call(self.client.account_info, escrow)["account"]
        except Exception as e:
            if is_not_found(e):
                print("  Escrow account has never been funded - skip")
                return None
            raise

        holdings = [{"asset-id": 0, "amount": acct.get("amount", 0)}]
        holdings += [
            {"asset-id": a["asset-id"], "amount": a.get("amount", 0)}
            for a in acct.get("assets", [])
            if not a.get("deleted")
        ]

        significant = []
        for h in holdings:
            meta = self.asset_meta(h["asset-id"])
            whole = h["amount"] / (10 ** meta["decimals"])
            if whole >= self.min_units:
                significant.append({**h, "decimals": meta["decimals"], "unit": meta["unit"], "whole": whole})

        if not significant:
            print("  No significant balances - skip")
            return None

        print(f"  Significant holdings: {len(significant)}")
        for s in significant:
            print(f"    {s['unit']:<10} (id {s['asset-id']}): {s['whole']:,.{min(s['decimals'], 6)}f}")

        # 3) Any app call inside the inactivity window? Use wall-clock time, not round estimates.
        since = datetime.now(timezone.utc) - timedelta(days=self.inactive_days)
        txns = call(
            self.client.search_transactions,
            application_id=app_id,
            start_time=since.strftime("%Y-%m-%dT%H:%M:%SZ"),
            limit=1,
        )
        recent = txns.get("transactions", [])
        inactive = not recent

        if inactive:
            print(f"  No app calls in last {self.inactive_days}d -> INACTIVE")
        else:
            t = recent[0]
            ts = datetime.fromtimestamp(t.get("round-time", 0), timezone.utc)
            print(f"  Active: app call(s) since {since:%Y-%m-%d} "
                  f"(e.g. round {t.get('confirmed-round')} at {ts:%Y-%m-%d %H:%M} UTC)")

        return {
            "app_id": app_id,
            "escrow": escrow,
            "creator": app.get("params", {}).get("creator"),
            "balances": [
                {"asset_id": s["asset-id"], "unit": s["unit"], "amount": s["amount"], "decimals": s["decimals"]}
                for s in significant
            ],
            "inactive": inactive,
        }


def main() -> int:
    p = argparse.ArgumentParser(description="Find inactive AVM DEX pools that still hold balances (read-only).")
    p.add_argument("app_ids", nargs="*", type=int, help="pool application IDs (default: KNOWN_POOLS)")
    p.add_argument("--indexer", default=INDEXER_URL, help=f"indexer URL (default {INDEXER_URL})")
    p.add_argument("--token", default=INDEXER_TOKEN, help="indexer API token, if any")
    p.add_argument("--days", type=int, default=INACTIVE_DAYS, help="inactivity window in days")
    p.add_argument("--min-units", type=float, default=MIN_WHOLE_UNITS, help="min whole tokens to count a balance")
    p.add_argument("--json", metavar="FILE", help="write inactive candidates to a JSON file")
    args = p.parse_args()

    app_ids = args.app_ids or KNOWN_POOLS
    headers = {"User-Agent": "avm-pool-scanner/1.0"}
    client = indexer.IndexerClient(args.token, args.indexer, headers=headers)

    try:
        health = call(client.health)
        print(f"Indexer {args.indexer} at round {health.get('round', '?')}")
    except Exception as e:
        print(f"Cannot reach indexer {args.indexer}: {e}", file=sys.stderr)
        return 1

    scanner = Scanner(client, args.days, args.min_units)
    print(f"Scanning {len(app_ids)} pools (inactive = no app calls in {args.days} days)...")

    candidates, errors = [], 0
    for i, app_id in enumerate(app_ids):
        try:
            result = scanner.check_pool(app_id)
        except Exception as e:
            errors += 1
            print(f"  Error on {app_id}: {e}")
            result = None
        if result and result["inactive"]:
            candidates.append(result)
        if i < len(app_ids) - 1:
            time.sleep(REQUEST_DELAY)

    print("\n" + "=" * 50)
    print(f"Inactive pools with balances: {len(candidates)}  (errors: {errors})")
    for c in candidates:
        units = ", ".join(b["unit"] for b in c["balances"])
        print(f"  App {c['app_id']} - {units}")

    if args.json:
        with open(args.json, "w") as f:
            json.dump(candidates, f, indent=2)
        print(f"Wrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
