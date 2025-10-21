# sol_multi_to_notion.py
# Python 3.11+, standard library only.
#
# ENV (GitHub Secrets -> Actions env):
#   NOTION_TOKEN            (required)  e.g., secret_xxx
#   NOTION_DB_PERWALLET    (required)  database id for "Per Wallet"
#   NOTION_DB_DAILYTOTAL   (required)  database id for "Daily Total"
#   WALLETS_CSV            (required)  comma-separated wallet addresses
#
# Optional:
#   SOLANA_RPC_URL         (default https://api.mainnet-beta.solana.com)
#   TOTAL_TITLE_PROP       (default "Name")  Title column name in Daily Total DB
#   TITLE_PROP_PERWALLET   (default "Wallet") Title column name in Per Wallet DB
#
# Per Wallet DB expected properties:
#   - Title column: "Wallet" (or set TITLE_PROP_PERWALLET)
#   - Date (Date)
#   - Start Balance (Number)  [optional, not filled in this simple version]
#   - End Balance (Number)
#   - Delta (Number)          [optional, not filled in this simple version]
#
# Daily Total DB expected properties:
#   - Title column: "Name" (or "Date" etc.; set TOTAL_TITLE_PROP to match)
#   - Date (Date)
#   - Start Balance (Number)  [optional]
#   - End Balance (Number)
#   - Delta (Number)          [optional]

import os, sys, json, urllib.request, urllib.error
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# ---------- Config ----------
RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com").strip()
NOTION_TOKEN = os.getenv("NOTION_TOKEN", "").strip()
DB_PER = os.getenv("NOTION_DB_PERWALLET", "").strip()
DB_TOTAL = os.getenv("NOTION_DB_DAILYTOTAL", "").strip()
WALLETS = [w.strip() for w in (os.getenv("WALLETS_CSV") or "").split(",") if w.strip()]

TITLE_PROP_PERWALLET = os.getenv("TITLE_PROP_PERWALLET", "Wallet").strip()
TOTAL_TITLE_PROP = os.getenv("TOTAL_TITLE_PROP", "Name").strip()  # Title column in Daily Total DB

# We'll use Australia/Brisbane for the "day" label if/when you add start/end aggregation later.
AEST = ZoneInfo("Australia/Brisbane")

# ---------- Utilities ----------
def fail(msg: str) -> None:
    print(f"ERROR: {msg}")
    sys.exit(1)

def notion_request(url: str, body: dict | None = None, method: str = "POST") -> dict:
    """Call Notion API and print detailed error body if Notion returns 4xx/5xx."""
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {NOTION_TOKEN}",
            "Content-Type": "application/json",
            "Notion-Version": "2022-06-28",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode() or "{}"
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        detail = e.read().decode()
        fail(f"Notion {e.code} {e.reason}: {detail}")

def rpc_get_balance(wallet: str) -> float:
    """Return SOL balance (in SOL) for a wallet via JSON-RPC getBalance."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [wallet, {"commitment": "finalized"}]}
    req = urllib.request.Request(
        RPC_URL,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        out = json.loads(resp.read().decode())
    try:
        lamports = out["result"]["value"]
    except Exception:
        fail(f"Unexpected RPC response for {wallet}: {out}")
    return lamports / 1_000_000_000

# ---------- Notion upserts ----------
def create_per_wallet_row(db_id: str, date_str: str, wallet: str, end_balance: float) -> None:
    """Create a simple per-wallet row (End Balance only)."""
    props = {
        "Date": {"date": {"start": date_str}},
        TITLE_PROP_PERWALLET: {"title": [{"text": {"content": wallet}}]},
        "End Balance": {"number": float(end_balance)},
        # You can also send Start Balance / Delta later when you aggregate.
    }
    body = {"parent": {"database_id": db_id}, "properties": props}
    notion_request("https://api.notion.com/v1/pages", body)

def create_daily_total_row(db_id: str, date_str: str, end_total: float) -> None:
    """
    Create a daily total row.
    IMPORTANT: we must also fill the Title column (TOTAL_TITLE_PROP) or Notion returns 400.
    """
    props = {
        TOTAL_TITLE_PROP: {"title": [{"text": {"content": date_str}}]},  # fill Title property (e.g., "Name" or "Date")
        "Date": {"date": {"start": date_str}},                            # Date-type column named exactly "Date"
        "End Balance": {"number": float(end_total)},
    }
    body = {"parent": {"database_id": db_id}, "properties": props}
    notion_request("https://api.notion.com/v1/pages", body)

# ---------- Main ----------
def main() -> None:
    # Validate required env
    if not NOTION_TOKEN: fail("NOTION_TOKEN missing")
    if not DB_PER: fail("NOTION_DB_PERWALLET missing")
    if not DB_TOTAL: fail("NOTION_DB_DAILYTOTAL missing")
    if not WALLETS: fail("WALLETS_CSV missing or empty")
    if not RPC_URL.startswith("https://"):
        fail(f"SOLANA_RPC_URL must be HTTPS; got '{RPC_URL}'")

    # Current date strings (UTC and AEST date label)
    now_utc = datetime.now(timezone.utc)
    today_local = now_utc.astimezone(AEST).date().isoformat()  # 'YYYY-MM-DD'

    # 1) Sample balances for all wallets
    per_wallet = []
    for w in WALLETS:
        bal = rpc_get_balance(w)
        per_wallet.append((w, bal))

    # 2) Write per-wallet rows
    for w, bal in per_wallet:
        create_per_wallet_row(DB_PER, today_local, w, bal)

    # 3) Write daily total row
    total = sum(b for _, b in per_wallet)
    create_daily_total_row(DB_TOTAL, today_local, total)

    # 4) Console info
    print(f"{today_local} | {len(WALLETS)} wallets written.")
    for w, b in per_wallet:
        print(f"  {w[:8]}…  End={b:.6f} SOL")
    print(f"TOTAL End={total:.6f} SOL")

if __name__ == "__main__":
    main()
