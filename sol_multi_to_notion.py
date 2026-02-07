import os
import csv
import requests
from datetime import datetime, timezone

# -----------------------------
# ENV
# -----------------------------
NOTION_TOKEN = os.environ["NOTION_TOKEN"]
DB_DAILY = os.environ["NOTION_DB_DAILYTOTAL"]
WALLETS_CSV = os.environ["WALLETS_CSV"]
RPC_URL = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")

HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Content-Type": "application/json",
    "Notion-Version": "2022-06-28"
}

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDC_DECIMALS = 6

# -----------------------------
# SOL RPC HELPERS
# -----------------------------
def rpc(method, params):
    res = requests.post(
        RPC_URL,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        timeout=20
    )
    res.raise_for_status()
    return res.json()["result"]

def get_sol_balance(wallet):
    lamports = rpc("getBalance", [wallet])["value"]
    return lamports / 1_000_000_000

def get_usdc_balance(wallet):
    accounts = rpc(
        "getTokenAccountsByOwner",
        [
            wallet,
            {"mint": USDC_MINT},
            {"encoding": "jsonParsed"}
        ]
    )["value"]

    total = 0.0
    for acc in accounts:
        amount = acc["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"]
        total += int(amount) / (10 ** USDC_DECIMALS)

    return round(total, 6)

# -----------------------------
# NOTION
# -----------------------------
def create_daily_row(sol, usdc):
    today = datetime.now(timezone.utc).date().isoformat()

    payload = {
        "parent": {"database_id": DB_DAILY},
        "properties": {
            "Name": {
                "title": [{"text": {"content": f"{sol:.2f} SOL"}}]
            },
            "Date": {"date": {"start": today}},
            "End Balance": {"number": round(sol, 6)},
            "USDC End Balance": {"number": round(usdc, 6)}
        }
    }

    r = requests.post("https://api.notion.com/v1/pages", headers=HEADERS, json=payload)
    if r.status_code != 200:
        raise Exception(r.text)

# -----------------------------
# MAIN
# -----------------------------
def main():
    wallets = []
    reader = csv.DictReader(WALLETS_CSV.strip().splitlines())
    for row in reader:
        wallets.append(row["wallet"].strip())

    total_sol = 0.0
    total_usdc = 0.0

    for w in wallets:
        total_sol += get_sol_balance(w)
        total_usdc += get_usdc_balance(w)

    create_daily_row(total_sol, total_usdc)
    print(f"OK → SOL={total_sol:.4f} USDC={total_usdc:.4f}")

if __name__ == "__main__":
    main()
