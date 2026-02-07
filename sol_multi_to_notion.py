import os
import csv
import json
import time
import urllib.request
from datetime import datetime, timezone

# =========================
# ENV
# =========================
NOTION_TOKEN = os.environ["NOTION_TOKEN"]
NOTION_DB_PERWALLET = os.environ["NOTION_DB_PERWALLET"]
NOTION_DB_DAILYTOTAL = os.environ["NOTION_DB_DAILYTOTAL"]
WALLETS_CSV = os.environ["WALLETS_CSV"]
SOLANA_RPC_URL = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")

USDC_MINT = "EPjFWdd5AufqSSqeM2q4Y9Jv6R3hHc3zZkZz8pJ9oG"

# =========================
# HELPERS
# =========================
def r2(x):
    return None if x is None else round(float(x), 2)

def rpc_post(payload):
    req = urllib.request.Request(
        SOLANA_RPC_URL,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())

def rpc_get_sol_balance(wallet):
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getBalance",
        "params": [wallet],
    }
    res = rpc_post(payload)
    return res["result"]["value"] / 1e9

def rpc_get_usdc_balance(wallet):
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
    total = 0.0
    for acc in res["result"]["value"]:
        total += float(
            acc["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"]
        )
    return total

def notion_headers():
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28",
    }

def notion_number(v):
    return None if v is None else {"number": v}

def notion_date(d):
    return {"date": {"start": d}}

def notion_create_page(db, props):
    body = {
        "parent": {"database_id": db},
        "properties": props,
    }
    req = urllib.request.Request(
        "https://api.notion.com/v1/pages",
        data=json.dumps(body).encode(),
        headers=notion_headers(),
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())

# =========================
# MAIN
# =========================
def main():
    wallets = []
    for w in WALLETS_CSV.split(","):
        w = w.strip()
        if w:
            wallets.append(w)

    if not wallets:
        raise Exception("No wallets provided")

    per_wallet = []
    total_sol = 0.0
    total_usdc = 0.0

    for w in wallets:
        sol = rpc_get_sol_balance(w)
        usdc = rpc_get_usdc_balance(w)

        sol = r2(sol)
        usdc = r2(usdc)

        per_wallet.append((w, sol, usdc))

        total_sol += sol
        total_usdc += usdc

    total_sol = r2(total_sol)
    total_usdc = r2(total_usdc)

    today = datetime.now(timezone.utc).date().isoformat()

    # =========================
    # PER WALLET PAGES
    # =========================
    for w, sol, usdc in per_wallet:
        props = {
            "Wallet": {"title": [{"text": {"content": w}}]},
            "Date": notion_date(today),
            "End Balance": notion_number(sol),
            "USDC End Balance": notion_number(usdc),
        }
        notion_create_page(NOTION_DB_PERWALLET, props)

    # =========================
    # DAILY TOTAL PAGE
    # =========================
    total_props = {
        "Name": {"title": [{"text": {"content": f"{total_sol} SOL"}}]},
        "Date": notion_date(today),
        "End Balance": notion_number(total_sol),
        "USDC End Balance": notion_number(total_usdc),
    }

    notion_create_page(NOTION_DB_DAILYTOTAL, total_props)

if __name__ == "__main__":
    main()
