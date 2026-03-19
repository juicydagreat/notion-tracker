import os
import re
import sys
import time
import json
import random
import urllib.request
import urllib.error

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
RPC_URL     = os.environ.get("SOLANA_RPC_URL", "").strip()
WALLETS_CSV = os.environ.get("WALLETS_CSV", "").strip()

RPC_TIMEOUT  = int(os.environ.get("RPC_TIMEOUT",    "30"))
RPC_RETRIES  = int(os.environ.get("RPC_RETRIES",     "8"))
DELAY_SOL    = float(os.environ.get("RPC_DELAY_SOL",   "0.4"))
BACKOFF_CAP  = float(os.environ.get("RPC_BACKOFF_CAP", "30.0"))

PUBKEY_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")

# ---------------------------------------------------------------------------
# Wallet parsing
# ---------------------------------------------------------------------------
def parse_wallets(raw: str) -> list:
    found = PUBKEY_RE.findall(raw or "")
    seen = set()
    result = []
    for w in found:
        if w not in seen:
            seen.add(w)
            result.append(w)
    return result

# ---------------------------------------------------------------------------
# RPC
# ---------------------------------------------------------------------------
def backoff_sleep(attempt: int) -> None:
    delay = min((2 ** attempt) + random.uniform(0.0, 0.8), BACKOFF_CAP)
    print(f"  [backoff] sleeping {delay:.2f}s before retry {attempt} ...", flush=True)
    time.sleep(delay)

def rpc_post(payload: dict) -> dict:
    last_err = None
    for attempt in range(RPC_RETRIES):
        try:
            req = urllib.request.Request(
                RPC_URL,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=RPC_TIMEOUT) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                data = json.loads(raw) if raw else {}
                if isinstance(data, dict) and data.get("error") is not None:
                    raise Exception(f"RPC error field: {data['error']}")
                return data
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            last_err = f"HTTP {e.code}: {detail}"
            if e.code == 429:
                print(f"  [429] rate limited: {detail}", flush=True)
                backoff_sleep(attempt + 1)
                continue
            if e.code in (408, 500, 502, 503, 504):
                print(f"  [transient {e.code}] retrying ...", flush=True)
                backoff_sleep(attempt + 1)
                continue
            raise Exception(last_err)
        except Exception as e:
            last_err = str(e)
            print(f"  [error] {last_err}", flush=True)
            backoff_sleep(attempt + 1)
    raise Exception(f"RPC failed after {RPC_RETRIES} attempts. Last: {last_err}")

def get_sol_balance(wallet: str) -> float:
    data = rpc_post({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getBalance",
        "params": [wallet],
    })
    lamports = data.get("result", {}).get("value", None)
    if lamports is None:
        print(f"  [warn] null result for {wallet}, counting as 0", flush=True)
        return 0.0
    return lamports / 1_000_000_000

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    if not RPC_URL:
        print("ERROR: SOLANA_RPC_URL is not set.")
        sys.exit(1)
    if not WALLETS_CSV:
        print("ERROR: WALLETS_CSV is not set.")
        sys.exit(1)
    if not RPC_URL.startswith("https://"):
        print(f"ERROR: SOLANA_RPC_URL does not look valid: {RPC_URL}")
        sys.exit(1)

    wallets = parse_wallets(WALLETS_CSV)

    print("=" * 60, flush=True)
    print(f"PARSED WALLET COUNT: {len(wallets)}", flush=True)
    print("=" * 60, flush=True)
    for i, w in enumerate(wallets, 1):
        print(f"  {i:>3}. {w}", flush=True)
    print(flush=True)

    if not wallets:
        print("ERROR: No valid Solana pubkeys found in WALLETS_CSV.")
        sys.exit(1)

    balances = {}
    errors = []

    for i, wallet in enumerate(wallets, 1):
        print(f"Fetching [{i:>3}/{len(wallets)}]: {wallet}", flush=True)
        try:
            sol = get_sol_balance(wallet)
            balances[wallet] = sol
            print(f"  -> {sol:.9f} SOL", flush=True)
        except Exception as e:
            print(f"  -> FAILED: {e}", flush=True)
            balances[wallet] = 0.0
            errors.append((wallet, str(e)))
        time.sleep(DELAY_SOL)

    print(flush=True)
    print("=" * 60, flush=True)
    print("SORTED BALANCES (descending)", flush=True)
    print("=" * 60, flush=True)
    sorted_balances = sorted(balances.items(), key=lambda x: x[1], reverse=True)
    for rank, (wallet, sol) in enumerate(sorted_balances, 1):
        print(f"  {rank:>3}. {wallet}  {sol:>16.9f} SOL", flush=True)

    total = sum(balances.values())
    zero_wallets = [w for w, s in balances.items() if s == 0.0]

    print(flush=True)
    print("=" * 60, flush=True)
    print(f"TOTAL SOL:         {total:.9f}", flush=True)
    print(f"WALLET COUNT:      {len(wallets)}", flush=True)
    print(f"ZERO-BALANCE:      {len(zero_wallets)}", flush=True)
    if zero_wallets:
        print("  Zero-balance wallets (check if intentional or fetch error):", flush=True)
        for w in zero_wallets:
            flag = " <- FETCH ERROR" if any(w == e[0] for e in errors) else ""
            print(f"    {w}{flag}", flush=True)
    if errors:
        print(f"FETCH ERRORS:      {len(errors)}", flush=True)
        for w, msg in errors:
            print(f"  {w}: {msg}", flush=True)
    print("=" * 60, flush=True)

if __name__ == "__main__":
    main()
