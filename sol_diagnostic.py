#!/usr/bin/env python3
"""
Diagnostic script — verify wallet parsing and SOL balances without touching Notion.
Uses the same batch RPC approach as sol_multi_to_notion.py.

Usage (local):
  WALLETS_CSV="addr1,addr2,..." python sol_diagnostic.py

Usage (GitHub Actions):
  Trigger workflow_dispatch with mode=diagnostic
"""
import os, sys, json, time, random, re
import urllib.request, urllib.error

SOLANA_RPC_URL  = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com").strip()
WALLETS_CSV     = os.environ.get("WALLETS_CSV", "")
RPC_TIMEOUT     = int(os.environ.get("RPC_TIMEOUT",     "30"))
RPC_RETRIES     = int(os.environ.get("RPC_RETRIES",     "5"))
RPC_BACKOFF_CAP = float(os.environ.get("RPC_BACKOFF_CAP", "30"))
BATCH_SIZE      = int(os.environ.get("BATCH_SIZE",       "50"))
BATCH_PAUSE     = float(os.environ.get("BATCH_PAUSE",    "1.0"))

PUBKEY_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")


def fail(msg): print(f"ERROR: {msg}", flush=True); sys.exit(1)
def log(msg):  print(msg, flush=True)


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


def chunks(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def rpc_call(payload):
    headers = {"Content-Type": "application/json"}
    last_err = None
    for attempt in range(RPC_RETRIES):
        try:
            req = urllib.request.Request(
                SOLANA_RPC_URL,
                data=json.dumps(payload).encode(),
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=RPC_TIMEOUT) as r:
                data = json.loads(r.read().decode("utf-8", errors="replace") or "{}")
            if isinstance(data, dict) and data.get("error"):
                raise Exception(f"RPC error: {data['error']}")
            return data
        except urllib.error.HTTPError as e:
            try:    detail = e.read().decode()
            except: detail = ""
            last_err = f"HTTP {e.code}: {detail}"
            if e.code == 429:
                log(f"  [429] rate limited")
                if "max usage reached" in detail:
                    raise Exception(f"RPC quota exhausted: {last_err}")
            elif e.code not in (408, 425, 500, 502, 503, 504):
                raise Exception(last_err)
            backoff(attempt)
        except Exception as ex:
            last_err = str(ex); log(f"  [rpc] {last_err}"); backoff(attempt)
    raise Exception(f"RPC failed after {RPC_RETRIES} attempts: {last_err}")


def batch_get_sol(wallets):
    results = {}
    indexed = list(enumerate(wallets))
    for i, chunk in enumerate(chunks(indexed, BATCH_SIZE)):
        if i > 0:
            time.sleep(BATCH_PAUSE)
        batch = [
            {"jsonrpc": "2.0", "id": idx, "method": "getBalance", "params": [w]}
            for idx, w in chunk
        ]
        resp = rpc_call(batch)
        if not isinstance(resp, list):
            raise Exception(f"Expected list from batch RPC, got: {type(resp)}")
        for item in resp:
            if item.get("error"):
                raise Exception(f"getBalance error for wallet #{item['id']}: {item['error']}")
            results[item["id"]] = item["result"]["value"] / 1e9
    return [results[i] for i in range(len(wallets))]


def main():
    if not SOLANA_RPC_URL.startswith("https://"):
        fail(f"SOLANA_RPC_URL must be https:// (got {SOLANA_RPC_URL})")

    wallets = parse_wallets(WALLETS_CSV)
    if not wallets:
        fail("No valid Solana pubkeys found in WALLETS_CSV")

    print("=" * 60)
    print(f"RPC:           {SOLANA_RPC_URL}")
    print(f"Wallet count:  {len(wallets)}")
    print(f"Batch size:    {BATCH_SIZE}")
    print("=" * 60)
    print()

    print("Fetching SOL balances (batch)...")
    sol_list = batch_get_sol(wallets)

    rows = sorted(zip(wallets, sol_list), key=lambda x: x[1], reverse=True)
    zero = [w for w, s in rows if s == 0]
    total = sum(s for _, s in rows)

    print()
    print("=" * 60)
    print("SORTED BALANCES")
    print("=" * 60)
    for i, (_, s) in enumerate(rows, 1):
        print(f"  {i:3d}.  {s:>14.9f} SOL")

    print()
    print("=" * 60)
    print(f"Total SOL:    {total:.9f}")
    print(f"Wallets:      {len(wallets)}")
    print(f"Zero-balance: {len(zero)}")
    if zero:
        print(f"  {len(zero)} zero-balance wallet(s) — check if intentional or fetch error")
    print("=" * 60)


if __name__ == "__main__":
    main()
