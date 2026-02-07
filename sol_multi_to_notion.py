import os
import csv
import json
import urllib.request
import urllib.error
from datetime import datetime, timezone

# -----------------------------
# ENV
# -----------------------------
NOTION_TOKEN = os.environ["NOTION_TOKEN"]
DB_DAILY = os.environ["NOTION_DB_DAILYTOTAL"]
WALLETS_CSV = os.environ["WALLETS_CSV"]

RPC_URL = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")

NOTION_VERSION = "2022-06-28"
NOTION_API_PAGES = "https://api.notion.com/v1/pages"

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDC_DECIMALS = 6

# -----------------------------
# HTTP HELPERS (urllib)
# -----------------------------
def http_post_json(url: str, payload: dict, headers: dict, timeout: int = 30) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))

def rpc(method: str, params: list) -> dict:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    headers = {"Content-Type": "application/json"}
    out = http_post_json(RPC_URL, payload, headers, timeout=30)
    if "error" in out:
        raise Exception(f"RPC error: {out['error']}")
    return out["result"]

# -----------------------------
# SOL / USDC
# -----------------------------
def get_sol_balance(wallet: str) -> float:
    lamports = rpc("getBalance", [wallet])["value"]
    return lamports / 1_000_000_000

def get_usdc_balance(wallet: str) -> float:
    res = rpc(
        "getTokenAccountsByOwner",
        [
            wallet,
            {"mint": USDC_MINT},
            {"encoding": "jsonParsed"}
        ]
    )

    total = 0.0
    for item in res.get("value", []):
        token_amount = (
            item["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"]
        )
        total += int(token_amount) / (10 ** USDC_DECIMALS)

    return round(total, 6)

# -----------------------------
# NOTION
# -----------------------------
def create_daily_row(sol_total: float, usdc_total: float) -> None:
    today = datetime.now(timezone.utc).date().isoformat()

    payload = {
        "parent": {"database_id": DB_DAILY},
        "properties": {
            # Your Daily Total DB MUST have a title property named "Name"
            "Name": {
                "title": [{"text": {"content": f"{sol_total:.2f} SOL"}}]
            },
            "Date": {"date": {"start": today}},
            "End Balance": {"number": round(sol_total, 6)},
            "USDC End Balance": {"number": round(usdc_total, 6)},
        },
    }

    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Content-Type": "application/json",
        "Notion-Version": NOTION_VERSION,
    }

    try:
        out = http_post_json(NOTION_API_PAGES, payload, headers, timeout=30)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise Exception(f"Notion HTTPError {e.code}: {body}") from None

    if out.get("object") == "error":
        raise Exception(f"Notion error: {out}")

# -----------------------------
# MAIN
# -----------------------------
def main():
    # WALLETS_CSV is stored as text in the secret. Must include header "wallet"
    wallets = []
    reader = csv.DictReader(WALLETS_CSV.strip().splitlines())
    for row in reader:
        w = (row.get("wallet") or "").strip()
        if w:
            wallets.append(w)

    if not wallets:
        raise Exception("No wallets found in WALLETS_CSV. Expected header: wallet")

    total_sol = 0.0
    total_usdc = 0.0

    for w in wallets:
        total_sol += get_sol_balance(w)
        total_usdc += get_usdc_balance(w)

    create_daily_row(total_sol, total_usdc)
    print(f"OK → SOL={total_sol:.6f} USDC={total_usdc:.6f}")

if __name__ == "__main__":
    main()
