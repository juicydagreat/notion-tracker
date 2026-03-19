import os
import sys
import json
import time
import random
import re
import urllib.request
import urllib.error
from datetime import datetime, timezone

# =========================
# ENV
# =========================
NOTION_TOKEN = os.environ["NOTION_TOKEN"].strip()
NOTION_DB_PERWALLET = os.environ["NOTION_DB_PERWALLET"].strip()
NOTION_DB_DAILYTOTAL = os.environ["NOTION_DB_DAILYTOTAL"].strip()
WALLETS_CSV = os.environ["WALLETS_CSV"]

SOLANA_RPC_URL = os.environ.get(
    "SOLANA_RPC_URL",
    "https://api.mainnet-beta.solana.com"
).strip()

USDC_WALLET = os.environ.get(
    "USDC_WALLET",
    "33EUErqH7mog7U2XdtXaZL7S1EEpJw1TEv7dswm76SzM"
).strip()

USDC_MINT = os.environ.get(
    "USDC_MINT",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
).strip()

NOTION_VERSION = os.environ.get("NOTION_VERSION", "2022-06-28").strip()

RPC_TIMEOUT = int(os.environ.get("RPC_TIMEOUT", "30"))
RPC_RETRIES = int(os.environ.get("RPC_RETRIES", "8"))
RPC_DELAY_SOL = float(os.environ.get("RPC_DELAY_SOL", "0.35"))
RPC_DELAY_USDC = float(os.environ.get("RPC_DELAY_USDC", "0.5"))
RPC_BACKOFF_CAP = float(os.environ.get("RPC_BACKOFF_CAP", "30"))

DEBUG = os.environ.get("DEBUG", "1").strip().lower() in ("1", "true", "yes", "y")

PUBKEY_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")


# =========================
# HELPERS
# =========================
def fail(msg: str) -> None:
    print(f"ERROR: {msg}")
    sys.exit(1)

def log(msg: str) -> None:
    print(msg, flush=True)

def r2(x):
    return None if x is None else round(float(x), 2)

def parse_wallets(raw: str) -> list[str]:
    found = PUBKEY_RE.findall(raw or "")
    out = []
    seen = set()
    for w in found:
        if w not in seen:
            seen.add(w)
            out.append(w)
    return out

def assert_pubkey(name: str, value: str) -> None:
    if not value or not PUBKEY_RE.fullmatch(value):
        fail(f"{name} is not a valid Solana pubkey: '{value}'")

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
                err = data["error"]
                raise Exception(f"RPC error: {err}")

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
                    raise Exception(
                        "RPC quota exhausted or project/key capped. "
                        f"Last error: {last_err}"
                    )
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

def rpc_get_usdc_balance(wallet: str) -> float:
    res = rpc_post({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTokenAccountsByOwner",
        "params": [
            wallet,
            {"mint": USDC_MINT},
            {"encoding": "jsonParsed"},
        ],
    })
    if "result" not in res or "value" not in res["result"]:
        raise Exception(f"RPC missing result for getTokenAccountsByOwner({wallet}): {res}")

    total = 0.0
    for acc in res["result"]["value"]:
        try:
            info = acc["account"]["data"]["parsed"]["info"]
            ta = info["tokenAmount"]
            ui_amt = ta.get("uiAmount")
            if ui_amt is not None:
                total += float(ui_amt)
            else:
                amt = int(ta.get("amount", "0"))
                dec = int(ta.get("decimals", 0))
                total += amt / (10 ** dec) if dec else float(amt)
        except Exception:
            continue

    return total

def notion_headers() -> dict:
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Content-Type": "application/json",
        "Notion-Version": NOTION_VERSION,
    }

def notion_req(url: str, body: dict, method: str = "POST") -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers=notion_headers(),
        method=method,
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw) if raw else {}
            if isinstance(data, dict) and data.get("object") == "error":
                raise Exception(f"Notion error: {data}")
            return data
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise Exception(f"Notion HTTP {e.code} {e.reason}: {detail}")

def create_page(db_id: str, props: dict) -> None:
    notion_req(
        "https://api.notion.com/v1/pages",
        {"parent": {"database_id": db_id}, "properties": props},
        method="POST",
    )


# =========================
# MAIN
# =========================
def main():
    if not NOTION_TOKEN:
        fail("NOTION_TOKEN missing")
    if not NOTION_DB_PERWALLET:
        fail("NOTION_DB_PERWALLET missing")
    if not NOTION_DB_DAILYTOTAL:
        fail("NOTION_DB_DAILYTOTAL missing")
    if not SOLANA_RPC_URL.startswith("https://"):
        fail(f"SOLANA_RPC_URL must be https:// (got {SOLANA_RPC_URL})")

    assert_pubkey("USDC_WALLET", USDC_WALLET)
    assert_pubkey("USDC_MINT", USDC_MINT)

    wallets = parse_wallets(WALLETS_CSV)
    if not wallets:
        fail("No valid Solana pubkeys found in WALLETS_CSV.")

    for w in wallets:
        assert_pubkey("Wallet", w)

    today = datetime.now(timezone.utc).date().isoformat()

    log("----- PARSED WALLETS -----")
    for i, w in enumerate(wallets, 1):
        log(f"{i:02d}: {w}")
    log(f"Wallets parsed: {len(wallets)}")
    log(f"Tracked USDC wallet: {USDC_WALLET}")

    log("----- FETCH USDC -----")
    usdc_total = r2(rpc_get_usdc_balance(USDC_WALLET))
    log(f"USDC wallet {USDC_WALLET} -> USDC={usdc_total}")
    time.sleep(RPC_DELAY_USDC)

    per_rows = []
    total_sol = 0.0

    log("----- FETCH SOL -----")
    for i, w in enumerate(wallets, 1):
        sol = r2(rpc_get_sol_balance(w))
        usdc = usdc_total if w == USDC_WALLET else 0.0

        per_rows.append((w, sol, usdc))
        total_sol += sol

        log(f"{i:02d}/{len(wallets)} | {w} | SOL={sol} | USDC={usdc}")
        time.sleep(RPC_DELAY_SOL)

    total_sol = r2(total_sol)

    log("----- SORTED SOL BALANCES -----")
    for w, sol, _ in sorted(per_rows, key=lambda x: x[1], reverse=True):
        log(f"{sol:>8.2f} | {w}")

    log("----- SUMMARY -----")
    log(f"Wallet count parsed: {len(wallets)}")
    log(f"Total SOL computed: {total_sol}")
    log(f"Total USDC computed: {usdc_total}")

    # Write per-wallet rows
    for w, sol, usdc in per_rows:
        create_page(NOTION_DB_PERWALLET, {
            "Wallet": {"title": [{"text": {"content": w}}]},
            "Date": {"date": {"start": today}},
            "End Balance": {"number": sol},
            "Delta": {"number": None},
            "USDC End Balance": {"number": usdc},
            "USDC Delta": {"number": None},
        })

    # Write daily total row
    create_page(NOTION_DB_DAILYTOTAL, {
        "Name": {"title": [{"text": {"content": f"{total_sol:.2f} SOL"}}]},
        "Date": {"date": {"start": today}},
        "End Balance": {"number": total_sol},
        "Delta": {"number": None},
        "USDC End Balance": {"number": usdc_total},
        "USDC Delta": {"number": None},
    })

    log("Done.")

if __name__ == "__main__":
    main()
