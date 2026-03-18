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
NOTION_TOKEN = os.environ["NOTION_TOKEN"].strip()
NOTION_DB_PERWALLET = os.environ["NOTION_DB_PERWALLET"].strip()
NOTION_DB_DAILYTOTAL = os.environ["NOTION_DB_DAILYTOTAL"].strip()
WALLETS_CSV = os.environ["WALLETS_CSV"]

SOLANA_RPC_URL = os.environ.get(
    "SOLANA_RPC_URL",
    "https://api.mainnet-beta.solana.com"
).strip()

# 🔥 ONLY wallet that holds USDC
USDC_WALLET = os.environ.get(
    "USDC_WALLET",
    "33EUErqH7mog7U2XdtXaZL7S1EEpJw1TEv7dswm76SzM"
).strip()

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

NOTION_VERSION = "2022-06-28"

PUBKEY_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")

# =========================
# HELPERS
# =========================
def r2(x):
    return None if x is None else round(float(x), 2)

def parse_wallets(raw: str) -> list[str]:
    found = PUBKEY_RE.findall(raw)
    return list(dict.fromkeys(found))

def rpc_post(payload: dict, retries: int = 8):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                SOLANA_RPC_URL,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )

            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode()
                return json.loads(raw)

        except urllib.error.HTTPError as e:
            body = e.read().decode()
            if "max usage reached" in body:
                raise Exception("RPC quota exhausted — check Helius usage")
            time.sleep(min(2 ** attempt, 30))

        except Exception:
            time.sleep(min(2 ** attempt, 30))

    raise Exception("RPC failed after retries")

def rpc_get_sol_balance(wallet: str) -> float:
    res = rpc_post({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getBalance",
        "params": [wallet]
    })
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

    total = 0.0
    for acc in res["result"]["value"]:
        info = acc["account"]["data"]["parsed"]["info"]
        ta = info["tokenAmount"]
        total += float(ta.get("uiAmount", 0))

    return total

def notion_headers():
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Content-Type": "application/json",
        "Notion-Version": NOTION_VERSION,
    }

def notion_req(url: str, body: dict):
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers=notion_headers(),
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())

def create_page(db_id: str, props: dict):
    notion_req("https://api.notion.com/v1/pages", {
        "parent": {"database_id": db_id},
        "properties": props
    })

# =========================
# MAIN
# =========================
def main():
    wallets = parse_wallets(WALLETS_CSV)

    if not wallets:
        raise Exception("No wallets found")

    today = datetime.now(timezone.utc).date().isoformat()

    # 🔥 Fetch USDC ONCE
    usdc_total = r2(rpc_get_usdc_balance(USDC_WALLET))

    per_rows = []
    total_sol = 0.0

    print(f"Fetching {len(wallets)} wallets...")

    for i, w in enumerate(wallets, 1):
        print(f"{i}/{len(wallets)}: {w}")

        sol = r2(rpc_get_sol_balance(w))
        total_sol += sol

        # assign USDC only to that wallet
        usdc = usdc_total if w == USDC_WALLET else 0.0

        per_rows.append((w, sol, usdc))

        time.sleep(0.4)  # mild throttle

    total_sol = r2(total_sol)

    # --------------------
    # WRITE PER WALLET
    # --------------------
    for w, sol, usdc in per_rows:
        create_page(NOTION_DB_PERWALLET, {
            "Wallet": {"title": [{"text": {"content": w}}]},
            "Date": {"date": {"start": today}},
            "End Balance": {"number": sol},
            "Delta": {"number": None},
            "USDC End Balance": {"number": usdc},
            "USDC Delta": {"number": None},
        })

    # --------------------
    # WRITE TOTAL
    # --------------------
    create_page(NOTION_DB_DAILYTOTAL, {
        "Name": {"title": [{"text": {"content": f"{total_sol:.2f} SOL"}}]},
        "Date": {"date": {"start": today}},
        "End Balance": {"number": total_sol},
        "Delta": {"number": None},
        "USDC End Balance": {"number": usdc_total},
        "USDC Delta": {"number": None},
    })

    print(f"Done. SOL={total_sol} USDC={usdc_total}")

if __name__ == "__main__":
    main()
