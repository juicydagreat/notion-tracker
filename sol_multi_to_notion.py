import os
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
NOTION_TOKEN = os.environ["NOTION_TOKEN"]
NOTION_DB_PERWALLET = os.environ["NOTION_DB_PERWALLET"]
NOTION_DB_DAILYTOTAL = os.environ["NOTION_DB_DAILYTOTAL"]
WALLETS_CSV = os.environ["WALLETS_CSV"]

SOLANA_RPC_URL = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com").strip()

# Correct USDC mint on Solana mainnet:
USDC_MINT = os.environ.get(
    "USDC_MINT",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
).strip()

NOTION_VERSION = "2022-06-28"

# Base58 pubkey regex (Solana excludes 0 O I l)
PUBKEY_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")


# =========================
# HELPERS
# =========================
def r2(x):
    return None if x is None else round(float(x), 2)

def parse_wallets(raw: str) -> list[str]:
    if not raw:
        return []
    found = PUBKEY_RE.findall(raw)
    out = []
    seen = set()
    for w in found:
        if w not in seen:
            seen.add(w)
            out.append(w)
    return out

def assert_pubkey(name: str, value: str):
    if not value or not PUBKEY_RE.fullmatch(value):
        raise Exception(f"{name} is not a valid Solana pubkey: '{value}'")

def rpc_post(payload: dict, retries: int = 8, timeout: int = 30) -> dict:
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(
                SOLANA_RPC_URL,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw)

            if isinstance(data, dict) and data.get("error") is not None:
                raise Exception(f"Solana RPC error: {data['error']}")

            return data

        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            last_err = f"HTTP {e.code}: {e.reason}. Body: {body[:300]}"
            if e.code not in (429, 500, 502, 503, 504):
                raise Exception(f"RPC hard failure: {last_err}")

        except Exception as e:
            last_err = str(e)

        time.sleep(min(2 ** attempt, 30) + random.uniform(0.0, 0.8))

    raise Exception(f"RPC failed after retries. Last error: {last_err}")

def rpc_get_sol_balance(wallet: str) -> float:
    payload = {"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [wallet]}
    res = rpc_post(payload)
    if "result" not in res:
        raise Exception(f"RPC missing result for getBalance({wallet}). Response: {res}")
    return res["result"]["value"] / 1e9

def rpc_get_usdc_balance(wallet: str) -> float:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTokenAccountsByOwner",
        "params": [
            wallet,
            {"mint": USDC_MINT},
            {"encoding": "jsonParsed"},
        ],
    }
    res = rpc_post(payload)
    if "result" not in res:
        raise Exception(f"RPC missing result for getTokenAccountsByOwner({wallet}). Response: {res}")

    total = 0.0
    for acc in res["result"]["value"]:
        info = acc["account"]["data"]["parsed"]["info"]
        ta = info["tokenAmount"]
        ui_amt = ta.get("uiAmount")
        if ui_amt is not None:
            total += float(ui_amt)
        else:
            # fallback to amount/decimals
            amt = int(ta.get("amount", "0"))
            dec = int(ta.get("decimals", 0))
            total += amt / (10 ** dec) if dec else float(amt)

    return total

def notion_headers():
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Content-Type": "application/json",
        "Notion-Version": NOTION_VERSION,
    }

def notion_date(d):
    return {"date": {"start": d}}

def notion_number(v):
    return {"number": v} if v is not None else {"number": None}

def notion_create_page(db_id, props):
    body = {"parent": {"database_id": db_id}, "properties": props}
    req = urllib.request.Request(
        "https://api.notion.com/v1/pages",
        data=json.dumps(body).encode("utf-8"),
        headers=notion_headers(),
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    data = json.loads(raw)
    if isinstance(data, dict) and data.get("object") == "error":
        raise Exception(f"Notion error: {data}")
    return data


# =========================
# MAIN
# =========================
def main():
    # Validate mint (this was the root cause)
    assert_pubkey("USDC_MINT", USDC_MINT)

    wallets = parse_wallets(WALLETS_CSV)
    if not wallets:
        raise Exception("No valid Solana pubkeys found in WALLETS_CSV.")

    today = datetime.now(timezone.utc).date().isoformat()

    per = []
    total_sol = 0.0
    total_usdc = 0.0

    for w in wallets:
        # Validate each wallet too
        assert_pubkey("Wallet", w)

        sol_raw = rpc_get_sol_balance(w)
        usdc_raw = rpc_get_usdc_balance(w)

        sol = r2(sol_raw)
        usdc = r2(usdc_raw)

        per.append((w, sol, usdc))
        total_sol += sol
        total_usdc += usdc

    total_sol = r2(total_sol)
    total_usdc = r2(total_usdc)

    # Per-wallet rows
    for w, sol, usdc in per:
        props = {
            "Wallet": {"title": [{"text": {"content": w}}]},
            "Date": notion_date(today),
            "End Balance": notion_number(sol),
            "USDC End Balance": notion_number(usdc),
        }
        notion_create_page(NOTION_DB_PERWALLET, props)

    # Daily total row
    total_props = {
        "Name": {"title": [{"text": {"content": f"{total_sol:.2f} SOL"}}]},
        "Date": notion_date(today),
        "End Balance": notion_number(total_sol),
        "USDC End Balance": notion_number(total_usdc),
    }
    notion_create_page(NOTION_DB_DAILYTOTAL, total_props)

    print(f"OK: wallets={len(wallets)} total_sol={total_sol:.2f} total_usdc={total_usdc:.2f}")

if __name__ == "__main__":
    main()
