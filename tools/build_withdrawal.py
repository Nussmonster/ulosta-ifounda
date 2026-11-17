#!/usr/bin/env python3
"""
Build UNSIGNED Nomadex remove-liquidity transactions for an account, verify them with
algod simulate, and write them to files for the account owner to review and sign.

Nothing is signed or sent. No keys are needed or accepted.

    python tools/build_withdrawal.py nussmonster.voi                # all LP positions, 100%
    python tools/build_withdrawal.py ADDRESS --pool 47424655 --percent 50
    python tools/build_withdrawal.py nussmonster.voi --out withdrawals/

Then sign and send with your own tools, e.g. with goal on your node:
    goal clerk sign -i withdraw-47424655.txn -o withdraw-47424655.stxn
    goal clerk rawsend -f withdraw-47424655.stxn

How the call works (observed on-chain, Nomadex pools on Voi):
  selector 0x542e0f0b, one uint256 argument = LP amount to burn. The pool burns that LP
  from the sender and pays out both sides via inner transactions. There is no
  minimum-out argument, so check the simulated payout before signing.
  Extra resource references, when more than one transaction can carry, go on preceding
  read-only arc200_symbol() calls to the same pool (the pattern Nomadex's UI uses).
"""
from __future__ import annotations

import argparse
import base64
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import pool_scanner as ps  # noqa: E402
from account_lookup import resolve  # noqa: E402
from algosdk import encoding, transaction  # noqa: E402
from algosdk.atomic_transaction_composer import (  # noqa: E402
    AtomicTransactionComposer, EmptySigner, TransactionWithSigner)
from algosdk.v2client import algod, indexer  # noqa: E402
from algosdk.v2client.models import SimulateRequest  # noqa: E402

REMOVE_LIQUIDITY = bytes.fromhex("542e0f0b")
ARC200_SYMBOL = bytes.fromhex("b6ae1a25")   # read-only; used only to carry references
MAX_REFS = 8                                 # accounts + apps + assets + boxes per txn
UA = {"User-Agent": "avm-pool-scanner/1.4"}


def simulate(client, txns, unnamed: bool) -> dict:
    atc = AtomicTransactionComposer()
    for t in txns:
        atc.add_transaction(TransactionWithSigner(t, EmptySigner()))
    req = SimulateRequest(txn_groups=[], allow_empty_signatures=True, allow_unnamed_resources=unnamed)
    return ps.call(atc.simulate, client, req).simulate_response


def collect_resources(sim: dict) -> dict:
    """Union of unnamed resources the group touched (group level and per transaction)."""
    out = {"accounts": set(), "apps": set(), "assets": set(), "boxes": set()}
    grp = sim["txn-groups"][0]
    blocks = [grp.get("unnamed-resources-accessed") or {}]
    blocks += [r.get("unnamed-resources-accessed") or {} for r in grp.get("txn-results", [])]
    for b in blocks:
        out["accounts"].update(b.get("accounts", []))
        out["apps"].update(b.get("apps", []))
        out["assets"].update(b.get("assets", []))
        for box in b.get("boxes", []):
            out["boxes"].add((box["app"], base64.b64decode(box.get("name", ""))))
        # app-locals / asset-holdings imply their account/app/asset
        for al in b.get("app-locals", []):
            out["accounts"].add(al["account"])
            out["apps"].add(al["app"])
        for ah in b.get("asset-holdings", []):
            out["accounts"].add(ah["account"])
            out["assets"].add(ah["asset"])
    return out


def build_group(sender, sp, pool_id, lp_amount, res) -> list:
    """Remove-liquidity call plus as many arc200_symbol() carriers as references need."""
    # A box of another app must name that app in the SAME transaction's app list, so each
    # such app travels with its boxes ("units"); everything else fills the remaining slots.
    other_boxes: dict[int, list[bytes]] = {}
    pool_boxes = []
    for a, n in sorted(res["boxes"]):
        if a in (0, pool_id):
            pool_boxes.append(n)
        else:
            other_boxes.setdefault(a, []).append(n)
    units = []
    for a, names in other_boxes.items():
        for i in range(0, len(names), MAX_REFS - 1):
            units.append([("app", a)] + [("box", (a, n)) for n in names[i:i + MAX_REFS - 1]])
    loose = ([("app", a) for a in sorted(set(res["apps"]) - set(other_boxes) - {pool_id})]
             + [("asset", a) for a in sorted(res["assets"])]
             + [("acct", a) for a in sorted(res["accounts"]) if a != sender]
             + [("box", (0, n)) for n in pool_boxes])
    chunks: list[list] = []
    for unit in units:
        target = next((c for c in chunks if len(c) + len(unit) <= MAX_REFS), None)
        if target is None:
            chunks.append(target := [])
        target.extend(unit)
    for ref in loose:
        target = next((c for c in chunks if len(c) < MAX_REFS), None)
        if target is None:
            chunks.append(target := [])
        target.append(ref)
    chunks = chunks or [[]]

    def make(args, chunk):
        return transaction.ApplicationNoOpTxn(
            sender, sp, pool_id, app_args=args,
            foreign_apps=[v for k, v in chunk if k == "app"] or None,
            foreign_assets=[v for k, v in chunk if k == "asset"] or None,
            accounts=[v for k, v in chunk if k == "acct"] or None,
            boxes=[v for k, v in chunk if k == "box"] or None)

    carriers = [make([ARC200_SYMBOL], c) for c in chunks[:-1]]
    main = make([REMOVE_LIQUIDITY, lp_amount.to_bytes(32, "big")], chunks[-1])
    txns = carriers + [main]
    if len(txns) > 1:
        transaction.assign_group_id(txns)
    return txns


def as_address(value) -> str | None:
    """algod JSON gives addresses as strings; accept base64 public keys too."""
    if not value:
        return None
    if encoding.is_valid_address(value):
        return value
    try:
        raw = base64.b64decode(value)
        return encoding.encode_address(raw) if len(raw) == 32 else None
    except Exception:
        return None


def payouts(sim: dict, sender: str) -> list[str]:
    """Human-readable list of what the inner transactions send to the sender."""
    sender_pk = encoding.decode_address(sender)
    out = []

    def walk(inner_list):
        for it in inner_list or []:
            txn = it.get("txn", {}).get("txn", {})
            ty = txn.get("type")
            if ty == "pay" and as_address(txn.get("rcv")) == sender:
                out.append(f"{ps.fmt_amount(txn.get('amt', 0), ps.NATIVE_DECIMALS)} VOI")
            elif ty == "axfer" and as_address(txn.get("arcv")) == sender:
                out.append(f"{txn.get('aamt', 0)} base units of ASA {txn.get('xaid')}")
            elif ty == "appl":
                args = [base64.b64decode(a) for a in txn.get("apaa", [])]
                if args and args[0] == bytes.fromhex("da7025b9") and len(args) == 3 and args[1] == sender_pk:
                    out.append(f"{int.from_bytes(args[2], 'big')} base units of ARC-200 app {txn.get('apid')}")
            walk(it.get("inner-txns"))

    for r in sim["txn-groups"][0]["txn-results"]:
        walk(r.get("txn-result", {}).get("inner-txns"))
    return out


def main() -> int:
    p = argparse.ArgumentParser(description="Build unsigned Nomadex remove-liquidity transactions (no signing).")
    p.add_argument("account", help="Voi address or enVoi .voi name")
    p.add_argument("--pool", type=int, action="append", help="pool app ID (repeatable); default: every LP position")
    p.add_argument("--percent", type=float, default=100.0, help="share of each LP position to withdraw (default 100)")
    p.add_argument("--out", default=".", help="directory for the unsigned .txn files")
    p.add_argument("--dry-run", action="store_true", help="simulate only; don't write files")
    args = p.parse_args()
    if not 0 < args.percent <= 100:
        p.error("--percent must be in (0, 100]")

    idx = indexer.IndexerClient("", ps.INDEXER_URL, headers=UA)
    alg = algod.AlgodClient("", ps.ALGOD_URL, headers=UA)
    sender = args.account if encoding.is_valid_address(args.account) else resolve(args.account)
    if not sender:
        print("Could not resolve the account.")
        return 1
    print(f"Account: {sender}")

    reader = ps.Arc200Reader(alg)
    pools = args.pool or ps.Nomadex(idx, ps.NOMADEX_FACTORY, None).list_pools()
    lp = {pid: b for pid, b in reader.balances(pools, holder=sender, sender=sender).items() if b > 0}
    if not lp:
        print("No LP positions found.")
        return 1

    os.makedirs(args.out, exist_ok=True)
    ok_all = True
    for pid, bal in lp.items():
        amount = bal if args.percent == 100 else int(bal * args.percent / 100)
        print(f"\n=== Pool {pid}: withdraw {amount} of {bal} LP base units ({args.percent:g}%)")
        sp = ps.call(alg.suggested_params)
        sp.flat_fee, sp.fee = True, 1000

        # Pass 1: let simulate fill in references, and record which it used.
        probe = build_group(sender, sp, pid, amount, {"accounts": set(), "apps": set(), "assets": set(), "boxes": set()})
        sim1 = simulate(alg, probe, unnamed=True)
        fail = sim1["txn-groups"][0].get("failure-message")
        if fail:
            print(f"  simulate failed: {fail}")
            ok_all = False
            continue
        res = collect_resources(sim1)

        # Pass 2: explicit references only, exactly as the files will contain.
        txns = build_group(sender, sp, pid, amount, res)
        sim2 = simulate(alg, txns, unnamed=False)
        fail = sim2["txn-groups"][0].get("failure-message")
        if fail:
            print(f"  strict simulate failed (not writing a file): {fail}")
            ok_all = False
            continue
        print(f"  verified by simulate: {len(txns)} transaction(s), total fee "
              f"{ps.fmt_amount(sum(t.fee for t in txns), 6)} VOI, valid rounds {sp.first}-{sp.last}")
        for line in payouts(sim2, sender):
            print(f"  you receive: {line}")

        if not args.dry_run:
            path = os.path.join(args.out, f"withdraw-{pid}.txn")
            transaction.write_to_file(txns, path)
            b64 = [encoding.msgpack_encode(t) for t in txns]
            with open(path + ".b64", "w") as f:
                f.write("\n".join(b64) + "\n")
            print(f"  wrote {path} (goal format) and {path}.b64 (base64, one per line)")
    print("\nNothing was signed or sent. Files expire after their last valid round (~45 min).")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
