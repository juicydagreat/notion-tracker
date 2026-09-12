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
MIN_SOL    = os.environ.get("MIN_SOL", "").strip()        # range mode: lower bound
MAX_SOL    = os.environ.get("MAX_SOL", "").strip()        # range mode: upper bound
SCAN_DEPTH = int(os.environ.get("SCAN_DEPTH",   "8"))     # txns per wallet to inspect
BEFORE_AEST = os.environ.get("BEFORE_AEST", "").strip()   # e.g. "2026-09-12 10:00" — ignore txns at/after
AFTER_AEST  = os.environ.get("AFTER_AEST", "").strip()    # e.g. "2026-09-12 00:00" — ignore txns before
LIST_ALL    = os.environ.get("LIST_ALL", "").strip().lower() in ("1", "true", "yes")  # list every tx in window


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

    before_cut = parse_before_cutoff(BEFORE_AEST)
    after_cut  = parse_before_cutoff(AFTER_AEST)

    # Range mode (MIN_SOL/MAX_SOL set) beats target±tolerance.
    if MIN_SOL or MAX_SOL:
        lo = float(MIN_SOL) if MIN_SOL else 0.0
        hi = float(MAX_SOL) if MAX_SOL else float("inf")
        amt_desc = f"between {lo:.2f} and {'∞' if hi == float('inf') else f'{hi:.2f}'} SOL"
    else:
        lo, hi = TARGET_SOL - TOLERANCE, TARGET_SOL + TOLERANCE
        amt_desc = f"~{TARGET_SOL} SOL  (window {lo:.2f}–{hi:.2f})"

    def fmt(ts, tz):
        return datetime.fromtimestamp(ts, tz=tz).strftime("%Y-%m-%d %H:%M")

    print("=" * 78)
    print(f"RPC:      {SOLANA_RPC_URL}")
    print(f"Wallets:  {len(wallets)}   |   scanning last {SCAN_DEPTH} txns each")
    if LIST_ALL:
        print("Mode:     LIST ALL transactions in the time window (any amount, in or out)")
    else:
        print(f"Mode:     OUTFLOWS {amt_desc}")
    if after_cut:
        print(f"From:     {fmt(after_cut, AEST)} AEST  ({fmt(after_cut, timezone.utc)} UTC)")
    if before_cut:
        print(f"To:       {fmt(before_cut, AEST)} AEST  ({fmt(before_cut, timezone.utc)} UTC)  (exclusive)")
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
            # keep only transactions inside [after_cut, before_cut)
            if (after_cut or before_cut) and bt is None:
                continue
            if before_cut and bt >= before_cut:
                continue
            if after_cut and bt < after_cut:
                continue
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
            others = [((post[j] - pre[j]) / 1e9, keys[j])
                      for j in range(min(len(pre), len(post), len(keys))) if keys[j] != w]
            if delta < 0:
                cp_amt, cp = max(others, default=(0.0, None))   # who received
            else:
                cp_amt, cp = min(others, default=(0.0, None))   # who sent
            where = "?" if cp is None else ("INTERNAL (your wallet)" if cp in wallet_set else "EXTERNAL")

            if LIST_ALL:
                hits.append((bt, w, delta, cp, where, sig))
                arrow = "->" if delta < 0 else "<-"
                log(f"  [{i:3d}/{len(wallets)}] {mask(w)}  {delta:+.4f} SOL  {arrow} {mask(cp) if cp else '?'}  [{where}]")
            else:
                out = -delta
                if out > 0 and lo <= out <= hi:
                    hits.append((bt, w, delta, cp, where, sig))
                    log(f"  [{i:3d}/{len(wallets)}] {mask(w)}  MATCH  {delta:+.4f} SOL  -> {mask(cp) if cp else '?'}  [{where}]")
        if i % 25 == 0:
            log(f"  ...scanned {i}/{len(wallets)}")

    print()
    print("=" * 78)
    title = "ALL ACTIVITY in window" if LIST_ALL else f"MATCHING OUTFLOWS {amt_desc}"
    print(f"{title}  ({len(hits)} found)")
    print("=" * 78)
    for bt, w, delta, cp, where, sig in sorted(hits, key=lambda x: (x[0] or 0), reverse=True):
        ts = fmt(bt, timezone.utc) + " UTC" if bt else "time?"
        verb, arrow = ("sent", "->") if delta < 0 else ("recv", "<-")
        print(f"  {ts}   {mask(w)}  {verb} {abs(delta):.4f} SOL  {arrow}  {mask(cp) if cp else '?'}  [{where}]")
        print(f"      tx: {sig}")
    if not hits:
        if LIST_ALL:
            print("  No transactions in that time window — no activity.")
        else:
            print("  No outflow near that amount in the scanned window.")
            print("  Try a wider range, a larger scan_depth, or LIST_ALL to see everything.")
    print("=" * 78)


if __name__ == "__main__":
    main()
