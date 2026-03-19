import os
import sys
import json
import time
import random
import re
import urllib.request
import urllib.error

SOLANA_RPC_URL = os.environ.get(
    "SOLANA_RPC_URL",
    "https://api.mainnet-beta.solana.com"
).strip()

WALLETS_CSV = os.environ.get("WALLETS_CSV", "")

RPC_TIMEOUT = int(os.environ.get("RPC_TIMEOUT", "30"))
RPC_RETRIES = int(os.environ.get("RPC_RETRIES", "6"))
RPC_DELAY_SOL = float(os.environ.get("RPC_DELAY_SOL", "0.25"))
RPC_BACKOFF_CAP = float(os.environ.get("RPC_BACKOFF_CAP", "20"))

PUBKEY_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")


def fail(msg: str) -> None:
    print(f"ERROR: {msg}", flush=True)
    sys.exit(1)


def log(msg: str) -> None:
    print(msg, flush=True)


def r4(x: float) -> float:
    return round(float(x), 4)


def parse_wallets(raw: str) -> list[str]:
    found = PUBKEY_RE.findall(raw or "")
    out = []
    seen = set()
    for w in found:
        if w not in seen:
            seen.add(w)
            out.append(w)
    return out


def backoff_sleep(attempt: int) -> None:
    delay = min((2 ** attempt) + random.uniform(0.0, 0.8), RPC_BACKOFF_CAP)
    log(f"Retrying after {delay:.2f}s...")
    time.sleep(delay)


def rpc_post(payload: dict) -> dict:
    last_err = None

    for attempt in range(RPC_RETRIES):
        try:
            req = urllib.request.Request(
                SOLANA_RPC_URL,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=RPC_TIMEOUT) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                data = json.loads(raw) if raw else {}

            if isinstance(data, dict) and data.get("error") is not None:
                raise Exception(f"RPC error: {data['error']}")

            return data

        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass

            last_err = f"HTTP {e.code} {e.reason}: {detail}"

            if e.code == 429:
                log(f"Rate limit hit: {last_err}")
                if "max usage reached" in detail:
                    raise Exception(f"RPC quota exhausted/capped: {last_err}")
                backoff_sleep(attempt + 1)
                continue

            if e.code in (408, 425, 500, 502, 503, 504):
                log(f"Transient RPC error: {last_err}")
                backoff_sleep(attempt + 1)
                continue

            raise Exception(last_err)

        except Exception as e:
            last_err = str(e)
            log(f"RPC request error: {last_err}")
            backoff_sleep(attempt + 1)

    raise Exception(f"RPC failed after retries. Last error: {last_err}")


def rpc_get_sol_balance(wallet: str) -> float:
    res = rpc_post({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getBalance",
        "params": [wallet],
    })
    if "result" not in res or "value" not in res["result"]:
        raise Exception(f"RPC missing result for getBalance({wallet}): {res}")
    return res["result"]["value"] / 1e9


def main():
    if not SOLANA_RPC_URL.startswith("https://"):
        fail(f"SOLANA_RPC_URL must be https:// (got {SOLANA_RPC_URL})")

    wallets = parse_wallets(WALLETS_CSV)
    if not wallets:
        fail("No valid Solana pubkeys found in WALLETS_CSV.")

    log("----- PARSED WALLETS -----")
    for i, w in enumerate(wallets, 1):
        log(f"{i:02d}: {w}")
    log(f"Wallets parsed: {len(wallets)}")

    rows = []
    total_sol = 0.0

    log("----- FETCH SOL -----")
    for i, w in enumerate(wallets, 1):
        sol = r4(rpc_get_sol_balance(w))
        rows.append((w, sol))
        total_sol += sol
        log(f"{i:02d}/{len(wallets)} | {w} | SOL={sol}")
        time.sleep(RPC_DELAY_SOL)

    total_sol = r4(total_sol)

    log("----- SORTED SOL BALANCES -----")
    for w, sol in sorted(rows, key=lambda x: x[1], reverse=True):
        log(f"{sol:>10.4f} | {w}")

    log("----- SUMMARY -----")
    log(f"Wallet count parsed: {len(wallets)}")
    log(f"Total SOL computed: {total_sol}")


if __name__ == "__main__":
    main()
