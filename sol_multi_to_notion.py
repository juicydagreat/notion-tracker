# sol_multi_to_notion.py
# This script checks multiple Solana wallets, logs balances,
# and sends data to Notion every hour.

import os, sys, json, csv, urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# Set up constants
RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
CSV_PATH = os.getenv("BALANCE_CSV_PATH", "balances.csv")

NOTION_TOKEN = os.getenv("NOTION_TOKEN", "").strip()
DB_PER = os.getenv("NOTION_DB_PERWALLET", "").strip()
DB_TOTAL = os.getenv("NOTION_DB_DAILYTOTAL", "").strip()
WALLETS = [w.strip() for w in (os.getenv("WALLETS_CSV") or "").split(",") if w.strip()]

AEST = ZoneInfo("Australia/Brisbane")

# Helper functions
def rpc_get_balance(wallet):
    payload = {"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [wallet]}
    data = json.dumps(payload).encode()
    req = urllib.request.Request(RPC_URL, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        out = json.loads(resp.read().decode())
    return out["result"]["value"] / 1_000_000_000

def notion_headers():
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28",
    }

def notion_query(db_id, date, wallet=None):
    filters = [{"property": "Date", "date": {"equals": date}}]
    if wallet:
        filters.append({"property": "Wallet", "title": {"contains": wallet[:6]}})
    body = {"filter": {"and": filters}, "page_size": 10}
    req = urllib.request.Request(
        f"https://api.notion.com/v1/databases/{db_id}/query",
        data=json.dumps(body).encode(),
        headers=notion_headers(),
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read()).get("results", [])

def notion_upsert(db_id, props, page_id=None):
    body = {"properties": props}
    if page_id:
        req = urllib.request.Request(
            f"https://api.notion.com/v1/pages/{page_id}",
            data=json.dumps(body).encode(),
            headers=notion_headers(),
            method="PATCH",
        )
    else:
        body["parent"] = {"database_id": db_id}
        req = urllib.request.Request(
            "https://api.notion.com/v1/pages",
            data=json.dumps(body).encode(),
            headers=notion_headers(),
        )
    with urllib.request.urlopen(req) as resp:
        _ = resp.read()

def main():
    if not (NOTION_TOKEN and DB_PER and DB_TOTAL and WALLETS):
        print("Missing secrets")
        sys.exit(1)

    now_utc = datetime.now(timezone.utc)
    today_local = now_utc.astimezone(AEST).date().isoformat()

    per_wallet_data = []
    for w in WALLETS:
        bal = rpc_get_balance(w)
        per_wallet_data.append((w, bal))

    # Summarize
    total_balance = sum(b for _, b in per_wallet_data)

    # Write to Notion per-wallet DB
    for w, bal in per_wallet_data:
        props = {
            "Date": {"date": {"start": today_local}},
            "Wallet": {"title": [{"text": {"content": w}}]},
            "End Balance": {"number": bal},
        }
        notion_upsert(DB_PER, props)

    # Write total to Notion total DB
    props_total = {
        "Date": {"date": {"start": today_local}},
        "End Balance": {"number": total_balance},
    }
    notion_upsert(DB_TOTAL, props_total)

    print(f"{today_local} | {len(WALLETS)} wallets updated to Notion.")

if __name__ == "__main__":
    main()
