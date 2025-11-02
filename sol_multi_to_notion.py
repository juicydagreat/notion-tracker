# sol_multi_to_notion.py
# Daily run (schedule via GitHub Actions). Computes delta vs the previous recorded entry in Notion.
#
# Required GitHub Secrets (Actions -> Secrets and variables):
#   NOTION_TOKEN            (Internal Integration token, starts with secret_)
#   NOTION_DB_PERWALLET    (DB id for "Per Wallet")
#   NOTION_DB_DAILYTOTAL   (DB id for "Daily Total")
#   WALLETS_CSV            (comma-separated wallet addresses)
#
# Optional:
#   SOLANA_RPC_URL         (default https://api.mainnet-beta.solana.com)
#   TITLE_PROP_PERWALLET   (default "Wallet")  # Title column name in Per-Wallet DB
#   TOTAL_TITLE_PROP       (default "Name")    # Title column name in Daily Total DB
#
# Expected Notion schema
# Per-Wallet DB:
#   - Title column: "Wallet" (or set TITLE_PROP_PERWALLET)
#   - Date (Date)
#   - End Balance (Number)
#   - Delta (Number)
#
# Daily-Total DB:
#   - Title column: "Name" (or "Date"; set TOTAL_TITLE_PROP if different)
#   - Date (Date)
#   - End Balance (Number)
#   - Delta (Number)

import os, sys, json, urllib.request, urllib.error
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Optional, Tuple

RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com").strip()
NOTION_TOKEN = os.getenv("NOTION_TOKEN", "").strip()
DB_PER = os.getenv("NOTION_DB_PERWALLET", "").strip()
DB_TOTAL = os.getenv("NOTION_DB_DAILYTOTAL", "").strip()
WALLETS = [w.strip() for w in (os.getenv("WALLETS_CSV") or "").split(",") if w.strip()]

TITLE_PROP_PERWALLET = os.getenv("TITLE_PROP_PERWALLET", "Wallet").strip()
TOTAL_TITLE_PROP = os.getenv("TOTAL_TITLE_PROP", "Name").strip()

# We label rows with the Australia/Brisbane date
AEST = ZoneInfo("Australia/Brisbane")

def fail(msg: str):
    print(f"ERROR: {msg}")
    sys.exit(1)

def notion_req(url: str, body: dict | None = None, method: str = "POST") -> dict:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "Authorization": f"Bearer {NOTION_TOKEN}",
            "Content-Type": "application/json",
            "Notion-Version": "2022-06-28",
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode() or "{}"
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        detail = e.read().decode()
        fail(f"Notion {e.code} {e.reason}: {detail}")

def rpc_get_balance(wallet: str) -> float:
    payload = {"jsonrpc":"2.0","id":1,"method":"getBalance","params":[wallet, {"commitment":"finalized"}]}
    req = urllib.request.Request(RPC_URL, data=json.dumps(payload).encode(),
                                 headers={"Content-Type":"application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        out = json.loads(resp.read().decode())
    try:
        lamports = out["result"]["value"]
    except Exception:
        fail(f"Unexpected RPC response for {wallet}: {out}")
    return lamports / 1_000_000_000

def per_wallet_latest_end(db_id: str, wallet: str) -> Optional[float]:
    body = {
        "filter": {"property": TITLE_PROP_PERWALLET, "title": {"equals": wallet}},
        "sorts": [{"property": "Date", "direction": "descending"}],
        "page_size": 1
    }
    res = notion_req(f"https://api.notion.com/v1/databases/{db_id}/query", body)
    results = res.get("results", [])
    if not results:
        return None
    props = results[0]["properties"]
    return props.get("End Balance", {}).get("number")

def latest_total_end_and_date(db_id: str) -> Tuple[Optional[float], Optional[str]]:
    body = {"sorts": [{"property": "Date", "direction": "descending"}], "page_size": 1}
    res = notion_req(f"https://api.notion.com/v1/databases/{db_id}/query", body)
    results = res.get("results", [])
    if not results:
        return None, None
    props = results[0]["properties"]
    end_prop = props.get("End Balance", {}).get("number")
    date_prop = props.get("Date", {}).get("date", {}).get("start")
    return end_prop, date_prop

def create_per_wallet_row(db_id: str, date_iso: str, wallet: str, end_balance: float, delta: Optional[float]):
    # Changed line: Title now shows balance (e.g. "138.46 SOL") instead of wallet address
    props = {
        "Date": {"date": {"start": date_iso}},
        TITLE_PROP_PERWALLET: {"title": [{"text": {"content": f"{round(end_balance, 2):.2f} SOL"}}]},
        "End Balance": {"number": round(end_balance, 2)},
    }
    if delta is not None:
        props["Delta"] = {"number": round(delta, 2)}
    body = {"parent": {"database_id": db_id}, "properties": props}
    notion_req("https://api.notion.com/v1/pages", body)

def create_daily_total_row(db_id: str, date_iso: str, end_total: float, delta_total: Optional[float]):
    title_text = f"{round(end_total, 2):.2f} SOL"
    props = {
        TOTAL_TITLE_PROP: {"title": [{"text": {"content": title_text}}]},
        "Date": {"date": {"start": date_iso}},
        "End Balance": {"number": round(end_total, 2)},
    }
    if delta_total is not None:
        props["Delta"] = {"number": round(delta_total, 2)}
    body = {"parent": {"database_id": db_id}, "properties": props}
    notion_req("https://api.notion.com/v1/pages", body)

def main():
    if not NOTION_TOKEN: fail("NOTION_TOKEN missing")
    if not DB_PER: fail("NOTION_DB_PERWALLET missing")
    if not DB_TOTAL: fail("NOTION_DB_DAILYTOTAL missing")
    if not WALLETS: fail("WALLETS_CSV missing or empty")
    if not RPC_URL.startswith("https://"):
        fail(f"SOLANA_RPC_URL must be HTTPS; got '{RPC_URL}'")

    now_utc = datetime.now(timezone.utc)
    today_aest = now_utc.astimezone(ZoneInfo("Australia/Brisbane")).date().isoformat()

    per = []
    for w in WALLETS:
        bal = rpc_get_balance(w)
        per.append((w, bal))

    per_rows = []
    for w, current in per:
        prev = per_wallet_latest_end(DB_PER, w)
        delta = None if prev is None else (current - prev)
        per_rows.append((w, current, delta))

    for w, end_bal, delta in per_rows:
        create_per_wallet_row(DB_PER, today_aest, w, end_bal, delta)

    end_total = sum(end for _, end, _ in per_rows)
    prev_total_end, _ = latest_total_end_and_date(DB_TOTAL)
    delta_total = None if prev_total_end is None else (end_total - prev_total_end)
    create_daily_total_row(DB_TOTAL, today_aest, end_total, delta_total)

    print(f"{today_aest} | {len(per_rows)} wallets")
    for w, end_bal, delta in per_rows:
        d = "None" if delta is None else f"{round(delta, 2):+.2f}"
        print(f"  {w[:8]}…  End={round(end_bal, 2):.2f}  Δ={d}")
    dt = "None" if delta_total is None else f"{round(delta_total, 2):+.2f}"
    print(f"TOTAL End={round(end_total, 2):.2f}  Δ={dt}")

if __name__ == "__main__":
    main()
