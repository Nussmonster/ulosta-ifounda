#!/usr/bin/env python3
"""
Probe Nomadex pool app calls on-chain to learn the method selectors and group layout
(read-only). Used to identify the remove-liquidity call.
"""
from __future__ import annotations

import base64
import collections
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import pool_scanner as ps  # noqa: E402
from algosdk import abi  # noqa: E402
from algosdk.v2client import indexer  # noqa: E402

POOLS = [47424655, 429995, 411796, 411756]
WINDOWS = 12          # 30-day windows, newest first
PER_WINDOW = 400
UA = {"User-Agent": "avm-pool-scanner-probe"}

CANDIDATES = [
    "swapAlphaToBeta(txn,uint256)uint256", "swapBetaToAlpha(txn,uint256)uint256",
    "addLiquidity(txn,txn,uint256)uint256", "addLiquidity(txn,txn)uint256",
    "addLiquidity(axfer,axfer,uint256)uint256", "addLiquidity(pay,axfer,uint256)uint256",
    "addLiquidity(uint256,uint256,uint256)uint256",
    "removeLiquidity(uint256)(uint256,uint256)", "removeLiquidity(uint256,uint256,uint256)(uint256,uint256)",
    "removeLiquidity(uint256)void", "removeLiquidity(uint256,uint256,uint256)void",
    "remove_liquidity(uint256)(uint256,uint256)", "removeLiquidity(txn,uint256,uint256)(uint256,uint256)",
    "burn(uint256)(uint256,uint256)", "withdraw(uint256)(uint256,uint256)",
    "arc200_transfer(address,uint256)bool", "arc200_transferFrom(address,address,uint256)bool",
    "arc200_approve(address,uint256)bool", "arc200_balanceOf(address)uint256",
    "createBalanceBox(address)void", "hasBox(address)bool",
]
SEL = {abi.Method.from_signature(s).get_selector().hex(): s for s in CANDIDATES}


def b64(x: str) -> bytes:
    return base64.b64decode(x)


def summarize_inner(t: dict, depth: int = 0) -> list[str]:
    out = []
    for it in t.get("inner-txns", []) or []:
        pad = "      " + "  " * depth
        ty = it.get("tx-type")
        if ty == "pay":
            p = it["payment-transaction"]
            out.append(f"{pad}inner pay {p['amount']} -> {p['receiver'][:8]}")
        elif ty == "axfer":
            a = it["asset-transfer-transaction"]
            out.append(f"{pad}inner axfer asset {a['asset-id']} amt {a['amount']} -> {a['receiver'][:8]}")
        elif ty == "appl":
            a = it["application-transaction"]
            args = [b64(x) for x in a.get("application-args", [])]
            sel = args[0][:4].hex() if args else ""
            out.append(f"{pad}inner appl app {a.get('application-id')} {SEL.get(sel, sel)} "
                       f"args {[x.hex() for x in args[1:]]}")
            out += summarize_inner(it, depth + 1)
        else:
            out.append(f"{pad}inner {ty}")
    return out


def describe(t: dict) -> list[str]:
    ty = t.get("tx-type")
    who = t["sender"][:8]
    if ty == "pay":
        p = t["payment-transaction"]
        return [f"    pay {p['amount']} {who} -> {p['receiver'][:8]} fee {t['fee']}"]
    if ty == "axfer":
        a = t["asset-transfer-transaction"]
        return [f"    axfer asset {a['asset-id']} amt {a['amount']} {who} -> {a['receiver'][:8]} fee {t['fee']}"]
    if ty == "appl":
        a = t["application-transaction"]
        args = [b64(x) for x in a.get("application-args", [])]
        sel = args[0][:4].hex() if args else ""
        lines = [f"    appl app {a.get('application-id')} {SEL.get(sel, sel)} from {who} fee {t['fee']} "
                 f"oc {a.get('on-completion')} args {[x.hex() for x in args[1:]]} "
                 f"apps {a.get('foreign-apps')} assets {a.get('foreign-assets')} "
                 f"accts {[x[:8] for x in a.get('accounts', [])]} boxes {len(a.get('box-references') or [])}"]
        logs = [b64(x) for x in (t.get("logs") or [])]
        if logs:
            lines.append(f"      last log {logs[-1].hex()[:200]}")
        return lines + summarize_inner(t)
    return [f"    {ty} from {who}"]


def main() -> int:
    idx = indexer.IndexerClient("", ps.INDEXER_URL, headers=UA)
    now = datetime.now(timezone.utc)
    for pid in POOLS:
        escrow = ps.get_application_address(pid)
        print(f"\n===== POOL {pid} escrow {escrow[:8]}")
        by_sel: dict[str, list[dict]] = collections.defaultdict(list)
        for w in range(WINDOWS):
            hi = now - timedelta(days=30 * w)
            lo = hi - timedelta(days=30)
            try:
                res = ps.call(idx.search_transactions, application_id=pid, start_time=ps.iso(lo),
                              end_time=ps.iso(hi), limit=PER_WINDOW)
            except Exception as e:
                print(f"  window {lo:%Y-%m-%d}: {e}")
                continue
            for t in res.get("transactions", []):
                if t.get("tx-type") != "appl":
                    continue
                args = t["application-transaction"].get("application-args", [])
                sel = b64(args[0])[:4].hex() if args else "(none)"
                by_sel[sel].append(t)
        for sel, ts in sorted(by_sel.items(), key=lambda kv: -len(kv[1])):
            print(f"\n  selector {sel} = {SEL.get(sel, '?')}: {len(ts)} calls")
            for t in ts[-2:]:  # two most recent examples
                print(f"   example round {t['confirmed-round']} group {t.get('group', '')[:12]}")
                if t.get("group"):
                    try:
                        grp = ps.call(idx.search_transactions, group_id=b64(t["group"]),
                                      round_num=t["confirmed-round"], limit=20)["transactions"]
                        grp.sort(key=lambda g: g.get("intra-round-offset", 0))
                    except Exception as e:
                        print(f"    group fetch failed: {e}")
                        grp = [t]
                else:
                    grp = [t]
                for g in grp:
                    print("\n".join(describe(g)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
