#!/usr/bin/env python3
"""
Diagnostic script — verify wallet count and SOL balances without touching Notion.
Wallet addresses are masked in output (AbCd...XyZ1) — safe for public repo logs.

Trigger via: Actions → Solana Notion Tracker → Run workflow → mode=diagnostic
"""
import os, sys, json, time, random, re
import urllib.request, urllib.error

SOLANA_RPC_URL  = os.environ.get("INDIVIDUAL_RPC", "https://api.mainnet-beta.solana.com").strip()
WALLETS_CSV     = os.environ.get("WALLETS_CSV", "")
RPC_TIMEOUT     = int(os.environ.get("RPC_TIMEOUT",      "30"))
RPC_RETRIES     = int(os.environ.get("RPC_RETRIES",      "5"))
RPC_BACKOFF_CAP = float(os.environ.get("RPC_BACKOFF_CAP", "30"))
INDIVIDUAL_DELAY = float(os.environ.get("INDIVIDUAL_DELAY", "2.0"))

PUBKEY_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")


def fail(msg): print(f"ERROR: {msg}", flush=True); sys.exit(1)
def log(msg):  print(msg, flush=True)
def mask(addr): return f"{addr[:4]}...{addr[-4:]}" if len(addr) >= 8 else addr


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


def get_sol_balance(wallet):
    payload = {"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [wallet]}
    last_err = None
    for attempt in range(RPC_RETRIES):
        try:
            req = urllib.request.Request(
                SOLANA_RPC_URL,
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=RPC_TIMEOUT) as r:
                data = json.loads(r.read().decode("utf-8", errors="replace") or "{}")
            if data.get("error"):
                raise Exception(f"RPC error: {data['error']}")
            return data["result"]["value"] / 1e9
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


def main():
    wallets = parse_wallets(WALLETS_CSV)
    if not wallets:
        fail("No valid Solana pubkeys found in WALLETS_CSV")

    print("=" * 60)
    print(f"RPC:          {SOLANA_RPC_URL}")
    print(f"Wallets:      {len(wallets)}")
    print(f"Delay:        {INDIVIDUAL_DELAY}s between calls")
    print("=" * 60)

    rows = []
    total = 0.0
    for i, wallet in enumerate(wallets, 1):
        sol = get_sol_balance(wallet)
        rows.append((wallet, sol))
        total += sol
        log(f"  [{i:3d}/{len(wallets)}] {sol:>12.4f} SOL  {mask(wallet)}")
        if i < len(wallets):
            time.sleep(INDIVIDUAL_DELAY)

    rows_sorted = sorted(rows, key=lambda x: x[1], reverse=True)
    zero = [w for w, s in rows if s == 0]

    print()
    print("=" * 60)
    print("SORTED BALANCES (descending)")
    print("=" * 60)
    for i, (w, s) in enumerate(rows_sorted, 1):
        print(f"  {i:3d}. {s:>12.4f} SOL  {mask(w)}")

    print()
    print("=" * 60)
    print(f"Total SOL:    {total:.4f}")
    print(f"Wallets:      {len(wallets)}")
    print(f"Zero-balance: {len(zero)}")
    print("=" * 60)


if __name__ == "__main__":
    main()
