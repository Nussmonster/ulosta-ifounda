#!/usr/bin/env python3
"""
Look up a Voi account (address or enVoi .voi name): balances, apps it created, and its
liquidity (LP) positions in Nomadex pools. Read-only.

    python tools/account_lookup.py nussmonster.voi
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import pool_scanner as ps  # noqa: E402
from algosdk import encoding  # noqa: E402
from algosdk.v2client import algod, indexer  # noqa: E402

ENVOI_HOSTS = ["https://api.envoi.sh", "https://envoi-api.nautilus.sh", "https://api.envoi.systems"]
UA = {"User-Agent": "avm-pool-scanner/1.3"}


def http_json(url: str):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def resolve(name: str) -> str | None:
    for host in ENVOI_HOSTS:
        url = f"{host}/api/address/{urllib.parse.quote(name)}"
        try:
            data = http_json(url)
        except Exception as e:
            print(f"  enVoi {host}: {type(e).__name__}: {str(e)[:120]}")
            continue
        print(f"  enVoi {host}: {json.dumps(data)[:400]}")
        for r in data.get("results", []) if isinstance(data, dict) else []:
            addr = r.get("address")
            if addr and encoding.is_valid_address(addr):
                return addr
    return None


def reverse(addr: str) -> None:
    for host in ENVOI_HOSTS:
        try:
            print(f"  reverse {host}: {json.dumps(http_json(f'{host}/api/name/{addr}'))[:300]}")
            return
        except Exception as e:
            print(f"  reverse {host}: {type(e).__name__}")


def main() -> int:
    target = sys.argv[1] if len(sys.argv) > 1 else "nussmonster.voi"
    idx = indexer.IndexerClient("", ps.INDEXER_URL, headers=UA)
    alg = algod.AlgodClient("", ps.ALGOD_URL, headers=UA)

    print(f"=== {target}")
    addr = target if encoding.is_valid_address(target) else resolve(target)
    if not addr:
        print("Could not resolve the name.")
        return 1
    print(f"Address: {addr}")
    reverse(addr)

    try:
        acct = ps.call(idx.account_info, addr, include_all=False)["account"]
    except Exception as e:
        print(f"Account lookup failed: {e}")
        return 1
    print(f"\nVOI: {ps.fmt_amount(acct.get('amount', 0), 6)} "
          f"(min balance {ps.fmt_amount(acct.get('min-balance', 0), 6)})")
    scanner = ps.Scanner(idx, 90, 0, None, [], False)
    for a in acct.get("assets", []):
        if a.get("amount"):
            m = scanner.asset_meta(a["asset-id"])
            print(f"  ASA {m['unit']:<10} (id {a['asset-id']}): {ps.fmt_amount(a['amount'], m['decimals'])}")
    created = acct.get("created-apps", []) or []
    print(f"Created apps: {len(created)} {[c['id'] for c in created][:20]}")
    created_assets = acct.get("created-assets", []) or []
    print(f"Created assets: {len(created_assets)} {[c['index'] for c in created_assets][:20]}")

    # LP positions: each Nomadex pool is itself an ARC-200 LP token.
    nmx = ps.Nomadex(idx, ps.NOMADEX_FACTORY, None)
    pools = nmx.list_pools()
    reader = ps.Arc200Reader(alg)
    print(f"\nNomadex LP positions (checked {len(pools)} pools):")
    try:
        bal = reader.balances(pools, holder=addr, sender=addr)
    except ps.SimulateSenderError as e:
        print(f"  cannot simulate as this account ({e})")
        return 1
    held = {p: b for p, b in bal.items() if b > 0}
    if not held:
        print("  none")
    pool_scanner = ps.Scanner(idx, 90, 0, reader, [], False, nmx, last_seen=False)
    for pid, lp in held.items():
        app = ps.call(idx.applications, pid)["application"]
        st = ps.decode_global_state(app)
        dec = ps.state_uint(st.get("decimals")) or 6
        # total_supply is the fixed maximum; LP the pool still holds itself is not issued.
        max_supply = ps.state_uint(st.get("total_supply")) or 0
        escrow = ps.get_application_address(pid)
        unissued = reader.balances([pid], holder=escrow, sender=addr).get(pid, 0)
        outstanding = max_supply - unissued
        share = lp / outstanding if outstanding > 0 else 0
        print(f"\n  pool {pid}: LP {ps.fmt_amount(lp, dec)} of {ps.fmt_amount(outstanding, dec)} outstanding "
              f"= {share * 100:.4f}% of the pool")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            info = pool_scanner.check_pool(pid)
        if not info:
            print("    (pool details unavailable)")
            continue
        pair = " / ".join(f"{t['symbol']} ({t['kind']})" for t in info.get("pair", []))
        print(f"    pair {pair}, fee {info.get('fee_percent')}%, "
              f"{'INACTIVE' if info['inactive'] else 'active'}; "
              f"last app call {(info.get('last_app_call') or {}).get('time', 'not found')}")
        for h in info["balances"]:
            mine = int(h["amount"] * share)
            print(f"    {h['unit']:<10} pool {ps.fmt_amount(h['amount'], h['decimals'])}"
                  f"  -> your share {ps.fmt_amount(mine, h['decimals'])}")
    for c in created:
        if c["id"] in pools:
            print(f"  (created Nomadex pool {c['id']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
