#!/usr/bin/env python3
"""
Dead/abandoned DEX pool scanner for Voi (or any AVM chain).

Read-only: it queries a public indexer, and reads ARC-200 balances by *simulating*
read-only contract calls on algod. Nothing is signed or submitted.

    pip install "py-algorand-sdk>=2.6"
    python pool_scanner.py                          # scan KNOWN_POOLS
    python pool_scanner.py 47166025 47424655        # scan specific app IDs
    python pool_scanner.py --arc200 390001 --days 180 --json out.json
    python pool_scanner.py --nomadex                # every pool created by the Nomadex factory

Balances checked per pool escrow:
  * native VOI and standard assets (ASAs)        -> from the indexer
  * ARC-200 smart-contract tokens                 -> arc200_balanceOf(escrow) via algod simulate
    Which ARC-200 tokens to check: every ID passed with --arc200, plus (unless --no-discover)
    app IDs found in the pool's global state that answer the ARC-200 interface.

Nomadex pools (global state "factory" == Nomadex factory app) are recognised automatically:
their token pair is looked up in the Mimir API (falling back to global-state keys), ARC-200 legs
are read via simulate, and the pair and fee are reported. --nomadex lists all factory pools.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from algosdk import abi
from algosdk.atomic_transaction_composer import AtomicTransactionComposer, EmptySigner
from algosdk.error import AlgodHTTPError, IndexerHTTPError
from algosdk.logic import get_application_address
from algosdk.v2client import algod, indexer
from algosdk.v2client.models import SimulateRequest

# --- Defaults (override with CLI flags) ---
INDEXER_URL = "https://mainnet-idx.voi.nodely.dev"
ALGOD_URL = "https://mainnet-api.voi.nodely.dev"
API_TOKEN = ""  # empty for the public Nodely free tier
NOMADEX_FACTORY = 411751  # Nomadex factory app on Voi mainnet
MIMIR_URL = "https://voi-mainnet-mimirapi.nftnavigator.xyz"
NOMADEX_FEE_SCALE = 10 ** 14  # swap_fee / platform_fee are uint256 fractions of 1e14

# Placeholder app IDs -- replace with real pool app IDs from an explorer / pool registry.
KNOWN_POOLS = [
    47166025,
    47424655,
    47165570,
]

INACTIVE_DAYS = 90       # flag if no app calls in this many days
MIN_WHOLE_UNITS = 1.0    # ignore holdings below this many whole tokens (decimals-aware)
NATIVE_DECIMALS = 6      # VOI / ALGO
REQUEST_DELAY = 0.3      # seconds between pools, to be polite to the free endpoints
MAX_RETRIES = 4
MAX_DISCOVERY_CANDIDATES = 12   # app IDs probed per pool from its global state
MAX_GROUP = 16                  # AVM transaction group limit
MAX_APP_ID = 2 ** 40            # plausible app-ID range when discovering from global state

ARC200_SYMBOL = abi.Method.from_signature("arc200_symbol()byte[8]")
ARC200_DECIMALS = abi.Method.from_signature("arc200_decimals()uint8")
ARC200_BALANCE_OF = abi.Method.from_signature("arc200_balanceOf(address)uint256")


def is_not_found(err: Exception) -> bool:
    # py-algorand-sdk's indexer client drops the HTTP status and keeps only the message.
    # Indexer says e.g. "no application found for application-id: N" / "no accounts found for address: X".
    msg = str(err).lower()
    return "not found" in msg or ("no " in msg and " found" in msg)


def call(fn, *args, **kwargs):
    """Call an indexer/algod method, retrying rate limits and transient errors."""
    for attempt in range(MAX_RETRIES + 1):
        try:
            return fn(*args, **kwargs)
        except IndexerHTTPError as e:
            if is_not_found(e) or attempt == MAX_RETRIES:
                raise
        except AlgodHTTPError as e:
            code = getattr(e, "code", None)
            transient = code is None or code == 429 or code >= 500
            if not transient or attempt == MAX_RETRIES:
                raise
        except OSError:  # network errors, timeouts
            if attempt == MAX_RETRIES:
                raise
        time.sleep(2 ** attempt)


def fmt_amount(amount: int, decimals: int) -> str:
    return f"{amount / (10 ** decimals):,.{min(decimals, 6)}f}"


class SimulateSenderError(Exception):
    """The account used as simulate sender can't cover the (simulated) fee."""


class Arc200Reader:
    """Reads ARC-200 metadata and balances with read-only simulated app calls."""

    def __init__(self, client: algod.AlgodClient, sender_override: str | None = None):
        self.client = client
        self.sender_override = sender_override
        self._meta: dict[int, dict | None] = {}   # app_id -> {"symbol","decimals"} or None (not ARC-200)
        self._sp = None
        self._sp_time = 0.0

    def _params(self):
        if self._sp is None or time.time() - self._sp_time > 60:
            self._sp = call(self.client.suggested_params)
            self._sp_time = time.time()
        return self._sp

    def _simulate(self, sender: str, calls: list[tuple[int, abi.Method, list]]) -> list:
        """Simulate a group of ABI calls; return their decoded return values."""
        atc = AtomicTransactionComposer()
        sp = self._params()
        for app_id, method, args in calls:
            atc.add_method_call(app_id=app_id, method=method, sender=sender, sp=sp,
                                signer=EmptySigner(), method_args=args)
        req = SimulateRequest(txn_groups=[], allow_empty_signatures=True, allow_unnamed_resources=True)
        res = call(atc.simulate, self.client, req)
        if res.failure_message:
            if "overspend" in res.failure_message or "below min" in res.failure_message:
                raise SimulateSenderError(res.failure_message)
            raise RuntimeError(res.failure_message)
        out = []
        for r in res.abi_results:
            if r.decode_error:
                raise RuntimeError(f"cannot decode return value: {r.decode_error}")
            out.append(r.return_value)
        return out

    def seed(self, app_id: int, symbol: str, decimals: int) -> None:
        """Pre-fill metadata known from another source (e.g. a DEX API)."""
        self._meta.setdefault(app_id, {"symbol": symbol, "decimals": decimals})

    def meta(self, app_id: int, sender: str) -> dict | None:
        """ARC-200 metadata for app_id, or None if the app doesn't speak ARC-200."""
        if app_id not in self._meta:
            sender = self.sender_override or sender
            try:
                symbol, decimals = self._simulate(
                    sender, [(app_id, ARC200_SYMBOL, []), (app_id, ARC200_DECIMALS, [])])
            except SimulateSenderError:
                raise  # not the token's fault; don't cache
            except AlgodHTTPError as e:
                if getattr(e, "code", None) == 400:   # e.g. app doesn't exist
                    self._meta[app_id] = None
                    return None
                raise
            except RuntimeError:
                self._meta[app_id] = None
                return None
            sym = bytes(symbol).rstrip(b"\x00").decode("utf-8", "replace").strip()
            self._meta[app_id] = {"symbol": sym or f"ARC200-{app_id}", "decimals": int(decimals)}
        return self._meta[app_id]

    def balances(self, token_ids: list[int], holder: str, sender: str) -> dict[int, int]:
        """arc200_balanceOf(holder) for each token. Tokens whose call fails are left out."""
        sender = self.sender_override or sender
        result: dict[int, int] = {}
        for i in range(0, len(token_ids), MAX_GROUP):
            chunk = token_ids[i:i + MAX_GROUP]
            try:
                values = self._simulate(sender, [(t, ARC200_BALANCE_OF, [holder]) for t in chunk])
                result.update(zip(chunk, (int(v) for v in values)))
            except (RuntimeError, AlgodHTTPError):
                # One bad token fails the whole group: retry one by one.
                for t in chunk:
                    try:
                        result[t] = int(self._simulate(sender, [(t, ARC200_BALANCE_OF, [holder])])[0])
                    except (RuntimeError, AlgodHTTPError) as e:
                        print(f"    ARC-200 {t}: balanceOf failed ({e})")
        return result


def global_state_app_candidates(app: dict, exclude: int) -> list[int]:
    """Plausible app IDs stored in a pool's global state (uints, or 8-byte big-endian values)."""
    seen: list[int] = []
    for kv in app.get("params", {}).get("global-state", []) or []:
        v = kv.get("value", {})
        nums = []
        if v.get("type") == 2:
            nums.append(int(v.get("uint", 0)))
        elif v.get("type") == 1 and v.get("bytes"):
            raw = base64.b64decode(v["bytes"])
            if len(raw) == 8:
                nums.append(int.from_bytes(raw, "big"))
        for n in nums:
            if 1000 < n < MAX_APP_ID and n != exclude and n not in seen:
                seen.append(n)
    return seen[:MAX_DISCOVERY_CANDIDATES]


def decode_global_state(app: dict) -> dict[str, int | bytes]:
    """Global state as {key: int | bytes}; keys decoded as UTF-8 where possible."""
    out: dict[str, int | bytes] = {}
    for kv in app.get("params", {}).get("global-state", []) or []:
        raw_key = base64.b64decode(kv.get("key", ""))
        key = raw_key.decode("utf-8", "replace")
        v = kv.get("value", {})
        out[key] = int(v.get("uint", 0)) if v.get("type") == 2 else base64.b64decode(v.get("bytes", ""))
    return out


def state_uint(value) -> int | None:
    """A global-state value as an integer (uint, or big-endian bytes up to 32 long)."""
    if isinstance(value, int):
        return value
    if isinstance(value, bytes) and 0 < len(value) <= 32:
        return int.from_bytes(value, "big")
    return None


class Nomadex:
    """Nomadex-specific lookups: factory pool listing, token pair, fees."""

    TOKEN_KEYS = (("tok_a", "tokA", "token_a", "tokenA", "token0", "token_0"),
                  ("tok_b", "tokB", "token_b", "tokenB", "token1", "token_1"))

    def __init__(self, client: indexer.IndexerClient, factory: int, mimir_url: str | None):
        self.client = client
        self.factory = factory
        self.mimir_url = mimir_url.rstrip("/") if mimir_url else None
        self._platform_fee: int | None = None
        self.mimir_failures = 0

    def is_pool(self, state: dict) -> bool:
        return state_uint(state.get("factory")) == self.factory

    def list_pools(self) -> list[int]:
        """Every live app created by the factory whose state points back at the factory."""
        creator = get_application_address(self.factory)
        pools, next_page = [], None
        while True:
            res = call(self.client.search_applications, creator=creator, limit=1000, next_page=next_page)
            for app in res.get("applications", []):
                if not app.get("deleted") and self.is_pool(decode_global_state(app)):
                    pools.append(app["id"])
            next_page = res.get("next-token")
            if not next_page or not res.get("applications"):
                return sorted(pools)

    def platform_fee(self) -> int:
        if self._platform_fee is None:
            try:
                app = call(self.client.applications, self.factory)["application"]
                self._platform_fee = state_uint(decode_global_state(app).get("platform_fee")) or 0
            except Exception:
                self._platform_fee = 0
        return self._platform_fee

    def fee_percent(self, state: dict) -> float | None:
        swap_fee = state_uint(state.get("swap_fee"))
        if swap_fee is None:
            return None
        return (swap_fee + self.platform_fee()) * 100 / NOMADEX_FEE_SCALE

    def _mimir_tokens(self, pool_id: int) -> list[dict] | None:
        if not self.mimir_url:
            return None
        url = f"{self.mimir_url}/dex/pools?" + urllib.parse.urlencode({"poolId": pool_id})
        req = urllib.request.Request(url, headers={"User-Agent": "avm-pool-scanner/1.2"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                pools = json.load(r).get("pools") or []
        except Exception:
            self.mimir_failures += 1
            return None
        if not pools:
            return None
        tokens = []
        for side in ("tokenA", "tokenB"):
            t = pools[0].get(side) or {}
            try:
                tid = int(t["id"])
            except (KeyError, TypeError, ValueError):
                return None
            kind = str(t.get("type") or "").lower()
            kind = "native" if tid == 0 or kind in ("voi", "native") else ("arc200" if kind == "arc200" else "asa")
            try:
                decimals = int(t.get("decimals"))
            except (TypeError, ValueError):
                decimals = None
            tokens.append({"id": tid, "kind": kind, "symbol": str(t.get("symbol") or ""), "decimals": decimals})
        return tokens

    def tokens(self, pool_id: int, state: dict) -> tuple[list[dict], str]:
        """The pool's two tokens and where they came from ("mimir", "state" or "unknown")."""
        tokens = self._mimir_tokens(pool_id)
        if tokens:
            return tokens, "mimir"
        ids = []
        for keys in self.TOKEN_KEYS:
            found = next((state[k] for k in keys if isinstance(state.get(k), int)), None)
            if found is None:
                return [], "unknown"
            ids.append(found)
        # Type unknown from state alone; the scanner probes ARC-200 and checks ASAs/native itself.
        return [{"id": i, "kind": "native" if i == 0 else "unknown", "symbol": "", "decimals": None}
                for i in ids], "state"


class Scanner:
    def __init__(self, client: indexer.IndexerClient, inactive_days: int, min_units: float,
                 arc200: Arc200Reader | None, arc200_ids: list[int], discover: bool,
                 nomadex: Nomadex | None = None):
        self.client = client
        self.inactive_days = inactive_days
        self.min_units = min_units
        self.arc200 = arc200
        self.arc200_ids = arc200_ids
        self.discover = discover
        self.nomadex = nomadex
        self._asset_cache: dict[int, dict] = {}
        self._app_exists: dict[int, bool] = {}

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

    def app_exists(self, app_id: int) -> bool:
        if app_id not in self._app_exists:
            try:
                app = call(self.client.applications, app_id)["application"]
                self._app_exists[app_id] = not app.get("deleted")
            except Exception as e:
                if not is_not_found(e):
                    raise
                self._app_exists[app_id] = False
        return self._app_exists[app_id]

    def arc200_holdings(self, app: dict, escrow: str, extra_ids: list[int] = ()) -> list[dict]:
        token_ids = list(self.arc200_ids)
        for t in extra_ids:
            if t not in token_ids:
                token_ids.append(t)
        if self.discover:
            for cand in global_state_app_candidates(app, exclude=app["id"]):
                if cand not in token_ids and self.app_exists(cand):
                    token_ids.append(cand)

        tokens = {}
        for t in token_ids:
            m = self.arc200.meta(t, sender=escrow)
            if m:
                tokens[t] = m
        if not tokens:
            return []

        holdings = []
        for t, amount in self.arc200.balances(list(tokens), holder=escrow, sender=escrow).items():
            holdings.append({"kind": "arc200", "id": t, "amount": amount,
                             "decimals": tokens[t]["decimals"], "unit": tokens[t]["symbol"]})
        return holdings

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

        state = decode_global_state(app)
        dex_info: dict = {}
        nomadex_arc200: list[int] = []
        if self.nomadex and self.nomadex.is_pool(state):
            tokens, source = self.nomadex.tokens(app_id, state)
            fee = self.nomadex.fee_percent(state)
            dex_info = {"dex": "nomadex", "pair_source": source,
                        "pair": [{"id": t["id"], "kind": t["kind"], "symbol": t["symbol"]} for t in tokens]}
            if fee is not None:
                dex_info["fee_percent"] = round(fee, 6)
            pair = " / ".join(f"{t['symbol'] or t['id']} ({t['kind']})" for t in tokens) or "pair unknown"
            fee_txt = f", fee {fee:.4g}%" if fee is not None else ""
            print(f"  Nomadex pool: {pair} [via {source}]{fee_txt}")
            for t in tokens:
                if t["kind"] in ("arc200", "unknown") and t["id"] != 0:
                    nomadex_arc200.append(t["id"])
                    if t["kind"] == "arc200" and self.arc200 and t["symbol"] and t["decimals"] is not None:
                        self.arc200.seed(t["id"], t["symbol"], t["decimals"])

        # 2) Native + ASA balances held by the application account
        try:
            acct = call(self.client.account_info, escrow)["account"]
        except Exception as e:
            if is_not_found(e):
                print("  Escrow account has never been funded - skip")
                return None
            raise

        # Native balance above the account's locked minimum balance (this is what a pool can trade).
        native = max(0, acct.get("amount", 0) - acct.get("min-balance", 0))
        holdings = [{"kind": "native", "id": 0, "amount": native,
                     "decimals": NATIVE_DECIMALS, "unit": "VOI"}]
        for a in acct.get("assets", []):
            if a.get("deleted"):
                continue
            meta = self.asset_meta(a["asset-id"])
            holdings.append({"kind": "asa", "id": a["asset-id"], "amount": a.get("amount", 0),
                             "decimals": meta["decimals"], "unit": meta["unit"]})

        # 3) ARC-200 balances (stored in each token contract, not in the escrow account)
        arc200_note = None
        if self.arc200:
            try:
                holdings += self.arc200_holdings(app, escrow, nomadex_arc200)
            except SimulateSenderError as e:
                arc200_note = "escrow can't fund a simulated call; pass --sim-sender <funded address>"
                print(f"  ARC-200 skipped: {arc200_note} ({e})")
            except Exception as e:
                arc200_note = f"ARC-200 lookup failed: {e}"
                print(f"  {arc200_note}")

        significant = [h for h in holdings if h["amount"] / (10 ** h["decimals"]) >= self.min_units]
        if not significant:
            print("  No significant balances - skip")
            return None

        print(f"  Significant holdings: {len(significant)}")
        for h in significant:
            print(f"    {h['kind']:<7} {h['unit']:<10} (id {h['id']}): {fmt_amount(h['amount'], h['decimals'])}")

        # 4) Any app call inside the inactivity window? Use wall-clock time, not round estimates.
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

        result = {
            "app_id": app_id,
            "escrow": escrow,
            "creator": app.get("params", {}).get("creator"),
            **dex_info,
            "balances": significant,
            "inactive": inactive,
        }
        if arc200_note:
            result["arc200_note"] = arc200_note
        return result


def main() -> int:
    p = argparse.ArgumentParser(description="Find inactive AVM DEX pools that still hold balances (read-only).")
    p.add_argument("app_ids", nargs="*", type=int, help="pool application IDs (default: KNOWN_POOLS)")
    p.add_argument("--indexer", default=INDEXER_URL, help=f"indexer URL (default {INDEXER_URL})")
    p.add_argument("--algod", default=ALGOD_URL, help=f"algod URL for ARC-200 reads (default {ALGOD_URL})")
    p.add_argument("--token", default=API_TOKEN, help="API token for indexer/algod, if any")
    p.add_argument("--days", type=int, default=INACTIVE_DAYS, help="inactivity window in days")
    p.add_argument("--min-units", type=float, default=MIN_WHOLE_UNITS, help="min whole tokens to count a balance")
    p.add_argument("--arc200", type=int, nargs="+", default=[], metavar="APP_ID",
                   help="ARC-200 token app IDs to check in every pool")
    p.add_argument("--no-discover", action="store_true",
                   help="don't look for ARC-200 token IDs in each pool's global state")
    p.add_argument("--no-arc200", action="store_true", help="skip ARC-200 balances entirely")
    p.add_argument("--sim-sender", metavar="ADDRESS",
                   help="funded address used as sender for simulated calls (default: the pool escrow)")
    p.add_argument("--nomadex", action="store_true",
                   help="scan every pool created by the Nomadex factory (plus any app IDs given)")
    p.add_argument("--nomadex-factory", type=int, default=NOMADEX_FACTORY, metavar="APP_ID",
                   help=f"Nomadex factory app ID (default {NOMADEX_FACTORY})")
    p.add_argument("--mimir", default=MIMIR_URL, help=f"Mimir API for Nomadex token pairs (default {MIMIR_URL})")
    p.add_argument("--no-mimir", action="store_true", help="don't call the Mimir API; use global state only")
    p.add_argument("--json", metavar="FILE", help="write inactive candidates to a JSON file")
    args = p.parse_args()

    headers = {"User-Agent": "avm-pool-scanner/1.2"}
    idx = indexer.IndexerClient(args.token, args.indexer, headers=headers)

    try:
        health = call(idx.health)
        print(f"Indexer {args.indexer} at round {health.get('round', '?')}")
    except Exception as e:
        print(f"Cannot reach indexer {args.indexer}: {e}", file=sys.stderr)
        return 1

    reader = None
    if not args.no_arc200:
        algod_client = algod.AlgodClient(args.token, args.algod, headers=headers)
        try:
            status = call(algod_client.status)
            print(f"Algod   {args.algod} at round {status.get('last-round', '?')}")
            reader = Arc200Reader(algod_client, args.sim_sender)
        except Exception as e:
            print(f"WARNING: cannot reach algod {args.algod} ({e}); ARC-200 balances will NOT be checked",
                  file=sys.stderr)

    nomadex = Nomadex(idx, args.nomadex_factory, None if args.no_mimir else args.mimir)
    app_ids = list(args.app_ids)
    if args.nomadex:
        try:
            found = nomadex.list_pools()
        except Exception as e:
            print(f"Cannot list Nomadex pools from factory {args.nomadex_factory}: {e}", file=sys.stderr)
            return 1
        print(f"Nomadex factory {args.nomadex_factory}: {len(found)} pools")
        app_ids += [a for a in found if a not in app_ids]
    elif not app_ids:
        app_ids = list(KNOWN_POOLS)
    if not app_ids:
        print("No pools to scan.")
        return 0

    scanner = Scanner(idx, args.days, args.min_units, reader, args.arc200, not args.no_discover, nomadex)
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
    if not reader and not args.no_arc200:
        print("  (ARC-200 balances were NOT checked - algod unreachable)")
    if nomadex.mimir_failures:
        print(f"  (Mimir API failed {nomadex.mimir_failures}x - those Nomadex pairs came from global state, if at all)")
    for c in candidates:
        units = ", ".join(f"{b['unit']} {fmt_amount(b['amount'], b['decimals'])}" for b in c["balances"])
        tag = " [nomadex]" if c.get("dex") == "nomadex" else ""
        print(f"  App {c['app_id']}{tag} - {units}")

    if args.json:
        with open(args.json, "w") as f:
            json.dump(candidates, f, indent=2)
        print(f"Wrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
