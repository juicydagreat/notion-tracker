#!/usr/bin/env python3
"""
Activity report — for each Solana wallet, find its most recent transaction
and rank the wallets by how recently they were active. Read-only; touches
neither Notion nor balances. Addresses masked for public logs.

Trigger via: Actions → Solana Notion Tracker → Run workflow → mode=activity
"""
import os, sys, json, time, random, re
import urllib.request, urllib.error
from datetime import datetime, timezone

SOLANA_RPC_URL   = os.environ.get("INDIVIDUAL_RPC", "https://api.mainnet-beta.solana.com").strip()
WALLETS_CSV      = os.environ.get("WALLETS_CSV", "")
RPC_TIMEOUT      = int(os.environ.get("RPC_TIMEOUT",      "30"))
RPC_RETRIES      = int(os.environ.get("RPC_RETRIES",      "5"))
RPC_BACKOFF_CAP  = float(os.environ.get("RPC_BACKOFF_CAP", "30"))
INDIVIDUAL_DELAY = float(os.environ.get("INDIVIDUAL_DELAY", "2.0"))
AMOUNTS_TOP_N    = int(os.environ.get("AMOUNTS_TOP_N", "20"))

PUBKEY_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")


def fail(msg): print(f"ERROR: {msg}", flush=True); sys.exit(1)
def log(msg):  print(msg, flush=True)
def mask(a):   return f"{a[:4]}...{a[-4:]}" if len(a) >= 8 else a


def parse_wallets(raw):
    seen, out = set(), []
    for w in PUBKEY_RE.findall(raw or ""):
        if w not in seen:
            seen.add(w)
            out.append(w)
    return out


def backoff(attempt):
    d = min(2 ** attempt + random.uniform(0, 0.8), RPC_BACKOFF_CAP)
    log(f"  Retrying in {d:.1f}s...")
    time.sleep(d)


def rpc(payload):
    last_err = None
    for attempt in range(RPC_RETRIES):
        try:
            req = urllib.request.Request(
                SOLANA_RPC_URL, data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=RPC_TIMEOUT) as r:
                data = json.loads(r.read().decode("utf-8", errors="replace") or "{}")
            if data.get("error"):
                raise Exception(f"RPC error: {data['error']}")
            return data["result"]
        except urllib.error.HTTPError as e:
            try:    detail = e.read().decode()
            except: detail = ""
            last_err = f"HTTP {e.code}: {detail}"
            if e.code in {408, 425, 429, 500, 502, 503, 504}:
                backoff(attempt)
            else:
                raise Exception(last_err)
        except Exception as ex:
            last_err = str(ex)
            backoff(attempt)
    raise Exception(f"RPC failed: {last_err}")


def last_activity(wallet):
    """Return (block_time_or_None, signature_or_None) of the most recent tx."""
    res = rpc({"jsonrpc": "2.0", "id": 1, "method": "getSignaturesForAddress",
               "params": [wallet, {"limit": 1}]})
    if not res:
        return None, None
    sig = res[0]
    return sig.get("blockTime"), sig.get("signature")


def ago(bt, now):
    if bt is None:
        return "unknown time"
    s = max(0, int(now - bt))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, _ = divmod(s, 60)
    if d: return f"{d}d {h}h ago"
    if h: return f"{h}h {m}m ago"
    return f"{m}m ago"


def last_tx_amount(wallet, sig):
    """Net SOL change for `wallet` in transaction `sig`
    (positive = received, negative = sent/fees). None if unavailable."""
    tx = rpc({"jsonrpc": "2.0", "id": 1, "method": "getTransaction",
              "params": [sig, {"maxSupportedTransactionVersion": 0, "encoding": "jsonParsed"}]})
    if not tx:
        return None
    meta = tx.get("meta") or {}
    pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
    keys = (tx.get("transaction") or {}).get("message", {}).get("accountKeys", [])
    for i, k in enumerate(keys):
        pk = k.get("pubkey") if isinstance(k, dict) else k
        if pk == wallet and i < len(pre) and i < len(post):
            return (post[i] - pre[i]) / 1e9
    return None


def main():
    wallets = parse_wallets(WALLETS_CSV)
    if not wallets:
        fail("No valid Solana pubkeys found in WALLETS_CSV")

    print("=" * 72)
    print(f"RPC:      {SOLANA_RPC_URL}")
    print(f"Wallets:  {len(wallets)}   (checking most recent transaction each)")
    print(f"Amounts:  fetching for the {AMOUNTS_TOP_N} most recent")
    print("=" * 72)

    now = datetime.now(timezone.utc).timestamp()
    rows = []
    for i, w in enumerate(wallets, 1):
        bt, sig = last_activity(w)
        rows.append((w, bt, sig))
        when = "no transactions" if sig is None else f"{ago(bt, now)}"
        log(f"  [{i:3d}/{len(wallets)}] {mask(w)}  {when}")
        if i < len(wallets):
            time.sleep(INDIVIDUAL_DELAY)

    # Newest first; wallets with no activity sink to the bottom.
    active = sorted([r for r in rows if r[1] is not None], key=lambda x: x[1], reverse=True)
    no_time = [r for r in rows if r[1] is None and r[2] is not None]   # tx exists, no blockTime
    never   = [r for r in rows if r[2] is None]

    # Fetch the SOL amount moved for the N most recent wallets only.
    amounts = {}
    if active:
        print(f"\n--- Fetching amounts for the {min(AMOUNTS_TOP_N, len(active))} most recent ---")
        for w, bt, sig in active[:AMOUNTS_TOP_N]:
            amounts[sig] = last_tx_amount(w, sig)
            time.sleep(INDIVIDUAL_DELAY)

    def amt_str(sig):
        a = amounts.get(sig)
        return f"{a:+.4f} SOL" if a is not None else ""

    print()
    print("=" * 72)
    print("MOST RECENTLY ACTIVE (newest first)")
    print("=" * 72)
    for i, (w, bt, sig) in enumerate(active, 1):
        ts = datetime.fromtimestamp(bt, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        print(f"  {i:3d}. {mask(w)}  {ago(bt, now):>12}   {ts}   {amt_str(sig):>16}   {sig[:16]}...")
    for w, bt, sig in no_time:
        print(f"     - {mask(w)}  recent tx (time unavailable)   {sig[:16]}...")

    print()
    if active:
        w, bt, sig = active[0]
        ts = datetime.fromtimestamp(bt, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        print(f"Most recent activity: {mask(w)}  ({ago(bt, now)}, {ts})  {amt_str(sig)}")
    print(f"Wallets with no transactions: {len(never)}")
    print("=" * 72)


if __name__ == "__main__":
    main()
