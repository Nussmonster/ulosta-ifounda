"""Temporary diagnostics: dump raw on-chain data for a few pools (read-only)."""
import base64, urllib.request
from algosdk.logic import get_application_address
from algosdk.v2client import indexer

idx = indexer.IndexerClient("", "https://mainnet-idx.voi.nodely.dev", headers={"User-Agent": "avm-pool-scanner-diag"})
POOLS = [40176866, 429995, 411756, 40176894, 48787612, 919439]
FACTORY = 411751

def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "avm-pool-scanner-diag"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read(3000).decode("utf-8", "replace")
    except Exception as e:
        return "ERR", repr(e)[:300]

def dump_state(app_id):
    app = idx.applications(app_id, include_all=True)["application"]
    p = app["params"]
    print(f"  creator={p.get('creator')} schema global={p.get('global-state-schema')} local={p.get('local-state-schema')}")
    for kv in p.get("global-state", []) or []:
        k = base64.b64decode(kv["key"]); v = kv["value"]
        if v["type"] == 2:
            print(f"    {k!r:40} uint {v['uint']}")
        else:
            b = base64.b64decode(v.get("bytes", ""))
            print(f"    {k!r:40} bytes[{len(b)}] hex={b.hex()} int={int.from_bytes(b,'big') if b else None}")

print("=== FACTORY", FACTORY); dump_state(FACTORY)
for pid in POOLS:
    esc = get_application_address(pid)
    print(f"\n=== POOL {pid} escrow {esc}")
    dump_state(pid)
    a = idx.account_info(esc, include_all=True)["account"]
    print(f"  amount={a.get('amount')} min-balance={a.get('min-balance')} total-assets-opted-in={a.get('total-assets-opted-in')} "
          f"total-apps-opted-in={a.get('total-apps-opted-in')} total-created-assets={a.get('total-created-assets')} "
          f"total-box-bytes={a.get('total-box-bytes')} total-boxes={a.get('total-boxes')}")
    for s in a.get("assets", []):
        try:
            prm = idx.asset_info(s["asset-id"])["asset"]["params"]
            meta = f"{prm.get('unit-name')} dec={prm.get('decimals')} creator={prm.get('creator')}"
        except Exception as e:
            meta = f"? {e}"
        print(f"    asset {s['asset-id']} amount={s.get('amount')} deleted={s.get('deleted')} [{meta}]")
    for c in a.get("created-assets", [])[:5]:
        print(f"    created-asset {c['index']} {c['params'].get('unit-name')} total={c['params'].get('total')} dec={c['params'].get('decimals')}")
    print("  mimir:", *get(f"https://voi-mainnet-mimirapi.nftnavigator.xyz/dex/pools?poolId={pid}"))
    # Ordering check: by address vs by application id
    t = idx.search_transactions(address=esc, limit=3)
    print("  address-search first 3:", [(x.get("confirmed-round"), x.get("tx-type")) for x in t.get("transactions", [])])
    t = idx.search_transactions(application_id=pid, limit=3)
    print("  app-search first 3:   ", [(x.get("confirmed-round"), x.get("tx-type")) for x in t.get("transactions", [])])
    t = idx.search_transactions(application_id=pid, min_round=24_000_000, limit=3)
    print("  app-search >=24.0M:   ", [(x.get("confirmed-round"), x.get("tx-type")) for x in t.get("transactions", [])])

print("\n=== enVoi resolve nussmonster.voi")
for host in ("https://api.envoi.sh", "https://envoi-api.nautilus.sh", "https://api.envoi.systems"):
    print(host, *get(f"{host}/api/address/nussmonster.voi"))
