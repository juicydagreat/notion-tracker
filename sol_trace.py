#!/usr/bin/env python3
"""
Trace mode — find a SOL transfer of ~TARGET_SOL across your wallets.

For each wallet it scans the last SCAN_DEPTH transactions, computes the
wallet's native SOL change in each, and flags any OUTFLOW close to
TARGET_SOL. For each hit it reports the time, exact amount, and the most
likely destination — marking whether that destination is one of your own
wallets (internal move) or an external address (left your control).

Read-only. Addresses masked. Trigger: Actions → Solana Notion Tracker →
Run workflow → mode=trace (set target_sol).
"""
import os, sys, json, time, random, re
import urllib.request, urllib.error
from datetime import datetime, timezone, timedelta

AEST = timezone(timedelta(hours=10))   # Sept is AEST (UTC+10), pre-daylight-saving

SOLANA_RPC_URL   = os.environ.get("INDIVIDUAL_RPC", "https://api.mainnet-beta.solana.com").strip()
WALLETS_CSV      = os.environ.get("WALLETS_CSV", "")
RPC_TIMEOUT      = int(os.environ.get("RPC_TIMEOUT",      "30"))
RPC_RETRIES      = int(os.environ.get("RPC_RETRIES",      "5"))
RPC_BACKOFF_CAP  = float(os.environ.get("RPC_BACKOFF_CAP", "30"))
INDIVIDUAL_DELAY = float(os.environ.get("INDIVIDUAL_DELAY", "0.4"))

TARGET_SOL = float(os.environ.get("TARGET_SOL", "5"))
TOLERANCE  = float(os.environ.get("TOLERANCE",  "1.0"))   # match window: TARGET ± this
SCAN_DEPTH = int(os.environ.get("SCAN_DEPTH",   "8"))     # txns per wallet to inspect
BEFORE_AEST = os.environ.get("BEFORE_AEST", "").strip()   # e.g. "2026-09-12 10:00" — ignore txns at/after


def parse_before_cutoff(s):
    """Parse an AEST 'YYYY-MM-DD HH:MM' (or with 'T') into a UTC epoch, or None."""
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=AEST).timestamp()
        except ValueError:
            continue
    fail(f"Could not parse BEFORE_AEST={s!r}; use 'YYYY-MM-DD HH:MM' (AEST)")

PUBKEY_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")


def fail(msg): print(f"ERROR: {msg}", flush=True); sys.exit(1)
def log(msg):  print(msg, flush=True)
def mask(a):   return f"{a[:4]}...{a[-4:]}" if len(a) >= 8 else a


def parse_wallets(raw):
    seen, out = set(), []
    for w in PUBKEY_RE.findall(raw or ""):
        if w not in seen:
            seen.add(w); out.append(w)
    return out


def backoff(attempt):
    time.sleep(min(2 ** attempt + random.uniform(0, 0.8), RPC_BACKOFF_CAP))


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
            last_err = str(ex); backoff(attempt)
    raise Exception(f"RPC failed: {last_err}")


def keys_and_balances(tx):
    meta = tx.get("meta") or {}
    pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
    raw_keys = (tx.get("transaction") or {}).get("message", {}).get("accountKeys", [])
    keys = [(k.get("pubkey") if isinstance(k, dict) else k) for k in raw_keys]
    return keys, pre, post


def main():
    wallets = parse_wallets(WALLETS_CSV)
    if not wallets:
        fail("No valid Solana pubkeys found in WALLETS_CSV")
    wallet_set = set(wallets)

    cutoff = parse_before_cutoff(BEFORE_AEST)

    lo, hi = TARGET_SOL - TOLERANCE, TARGET_SOL + TOLERANCE
    print("=" * 78)
    print(f"RPC:      {SOLANA_RPC_URL}")
    print(f"Wallets:  {len(wallets)}   |   scanning last {SCAN_DEPTH} txns each")
    print(f"Looking for OUTFLOWS of {TARGET_SOL} SOL  (match window {lo:.2f}–{hi:.2f} SOL)")
    if cutoff:
        c_aest = datetime.fromtimestamp(cutoff, tz=AEST).strftime("%Y-%m-%d %H:%M AEST")
        c_utc  = datetime.fromtimestamp(cutoff, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        print(f"Cutoff:   ignoring transactions at/after {c_aest}  ({c_utc})")
    print("=" * 78)

    hits = []
    for i, w in enumerate(wallets, 1):
        try:
            sigs = rpc({"jsonrpc": "2.0", "id": 1, "method": "getSignaturesForAddress",
                        "params": [w, {"limit": SCAN_DEPTH}]}) or []
        except Exception as e:
            log(f"  [{i:3d}/{len(wallets)}] {mask(w)}  (sig fetch failed: {e})")
            continue
        for s in sigs:
            sig = s.get("signature")
            if not sig:
                continue
            bt = s.get("blockTime")
            if cutoff and (bt is None or bt >= cutoff):
                continue   # ignore transactions at/after the cutoff
            time.sleep(INDIVIDUAL_DELAY)
            try:
                tx = rpc({"jsonrpc": "2.0", "id": 1, "method": "getTransaction",
                          "params": [sig, {"maxSupportedTransactionVersion": 0, "encoding": "jsonParsed"}]})
            except Exception:
                continue
            if not tx:
                continue
            keys, pre, post = keys_and_balances(tx)
            if w not in keys:
                continue
            wi = keys.index(w)
            if wi >= len(pre) or wi >= len(post):
                continue
            delta = (post[wi] - pre[wi]) / 1e9          # negative = SOL left this wallet
            out = -delta                                 # positive = SOL left this wallet
            if out > 0 and lo <= out <= hi:
                # destination = account that gained the most SOL in this tx
                gains = [((post[j] - pre[j]) / 1e9, keys[j]) for j in range(min(len(pre), len(post), len(keys)))]
                gains = [(g, k) for g, k in gains if k != w]
                dest_amt, dest = max(gains, default=(0.0, None))
                where = "?" if dest is None else ("INTERNAL (your wallet)" if dest in wallet_set else "EXTERNAL")
                hits.append((s.get("blockTime"), w, out, dest, where, sig))
                log(f"  [{i:3d}/{len(wallets)}] {mask(w)}  MATCH  -{out:.4f} SOL  -> {mask(dest) if dest else '?'}  [{where}]")
        if i % 25 == 0:
            log(f"  ...scanned {i}/{len(wallets)}")

    print()
    print("=" * 78)
    print(f"MATCHES for ~{TARGET_SOL} SOL outflow ({len(hits)} found)")
    print("=" * 78)
    for bt, w, out, dest, where, sig in sorted(hits, key=lambda x: (x[0] or 0), reverse=True):
        ts = datetime.fromtimestamp(bt, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if bt else "time?"
        print(f"  {ts}   {mask(w)}  sent -{out:.4f} SOL  ->  {mask(dest) if dest else '?'}  [{where}]")
        print(f"      tx: {sig}")
    if not hits:
        print("  No outflow near that amount in the scanned window.")
        print(f"  Try a wider TOLERANCE or larger SCAN_DEPTH.")
    print("=" * 78)


if __name__ == "__main__":
    main()
