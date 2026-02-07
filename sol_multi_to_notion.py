# sol_multi_to_notion.py
# Daily run (schedule via GitHub Actions). Computes delta vs the previous recorded entry in Notion.
#
# Required GitHub Secrets (Actions -> Secrets and variables):
#   NOTION_TOKEN            (Internal Integration token, starts with secret_)
#   NOTION_DB_PERWALLET     (DB id for "Per Wallet")
#   NOTION_DB_DAILYTOTAL    (DB id for "Daily Total")
#   WALLETS_CSV             (comma-separated wallet addresses)
#
# Optional:
#   SOLANA_RPC_URL          (default https://api.mainnet-beta.solana.com)
#   TITLE_PROP_PERWALLET    (default "Wallet")        # Title column name in Per-Wallet DB
#   TOTAL_TITLE_PROP        (default "Name")          # Title column name in Daily Total DB
#   WALLET_ADDR_PROP        (default "Wallet Address")# Rich-text column used to store wallet address (Per-Wallet DB)
#   USDC_MINT               (default mainnet USDC mint)
#
# Expected Notion schema
# Per-Wallet DB:
#   - Title column: TITLE_PROP_PERWALLET (we write "{sol} SOL" to this title for display)
#   - WALLET_ADDR_PROP (Rich text): actual wallet address (used for querying prev entries)
#   - Date (Date)
#   - End Balance (Number)              # SOL end
#   - Delta (Number)                    # SOL delta
#   - USDC End Balance (Number)         # USDC end
#   - USDC Delta (Number)               # USDC delta
#
# Daily-Total DB:
#   - Title column: TOTAL_TITLE_PROP (we write "{total_sol} SOL" for calendar display)
#   - Date (Date)
#   - End Balance (Number)              # total SOL end
#   - Delta (Number)                    # total SOL delta
#   - USDC End Balance (Number)         # total USDC end
#   - USDC Delta (Number)               # total USDC delta

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

# This is the critical fix: store wallet address in a stable non-title field.
WALLET_ADDR_PROP = os.getenv("WALLET_ADDR_PROP", "Wallet Address").strip()

# Mainnet USDC mint
USDC_MINT = os.getenv("USDC_MINT", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v").strip()

AEST = ZoneInfo("Australia/Brisbane")


def fail(msg: str):
    print(f"ERROR: {msg}")
    sys.exit(1)


def notion_req(url: str, body: dict | None = None, method: str = "POST") -> dict:
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


def rpc_post(payload: dict) -> dict:
    req = urllib.request.Request(
        RPC_URL,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def rpc_get_balance(wallet: str) -> float:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getBalance",
        "params": [wallet, {"commitment": "finalized"}],
    }
    out = rpc_post(payload)
    try:
        lamports = out["result"]["value"]
    except Exception:
        fail(f"Unexpected RPC response for SOL balance {wallet}: {out}")
    return lamports / 1_000_000_000


def rpc_get_spl_mint_balance(owner: str, mint: str) -> float:
    """
    Sum UI balances across all token accounts owned by 'owner' for a given mint.
    Uses getTokenAccountsByOwner with jsonParsed encoding.
    """
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTokenAccountsByOwner",
        "params": [owner, {"mint": mint}, {"encoding": "jsonParsed"}],
    }
    out = rpc_post(payload)
    try:
        accounts = out["result"]["value"]
    except Exception:
        fail(f"Unexpected RPC response for token accounts owner={owner} mint={mint}: {out}")

    total = 0.0
    for acc in accounts:
        try:
            info = acc["account"]["data"]["parsed"]["info"]
            ui_amt = info["tokenAmount"]["uiAmount"]
            total += float(ui_amt or 0.0)
        except Exception:
            # Skip any unexpected shapes safely
            continue
    return total


def per_wallet_latest_numbers(db_id: str, wallet: str) -> Tuple[Optional[float], Optional[float]]:
    """
    Returns (prev_sol_end, prev_usdc_end) for this wallet, or (None, None) if no prior rows exist.
    We query by WALLET_ADDR_PROP, not the Title, because Title is used for display.
    """
    body = {
        "filter": {"property": WALLET_ADDR_PROP, "rich_text": {"equals": wallet}},
        "sorts": [{"property": "Date", "direction": "descending"}],
        "page_size": 1,
    }
    res = notion_req(f"https://api.notion.com/v1/databases/{db_id}/query", body)
    results = res.get("results", [])
    if not results:
        return None, None

    props = results[0]["properties"]
    prev_sol = props.get("End Balance", {}).get("number")
    prev_usdc = props.get("USDC End Balance", {}).get("number")
    return prev_sol, prev_usdc


def latest_total_numbers(db_id: str) -> Tuple[Optional[float], Optional[float]]:
    """
    Returns (prev_total_sol_end, prev_total_usdc_end), or (None, None) if no prior rows exist.
    """
    body = {"sorts": [{"property": "Date", "direction": "descending"}], "page_size": 1}
    res = notion_req(f"https://api.notion.com/v1/databases/{db_id}/query", body)
    results = res.get("results", [])
    if not results:
        return None, None

    props = results[0]["properties"]
    prev_sol = props.get("End Balance", {}).get("number")
    prev_usdc = props.get("USDC End Balance", {}).get("number")
    return prev_sol, prev_usdc


def create_per_wallet_row(
    db_id: str,
    date_iso: str,
    wallet: str,
    end_sol: float,
    delta_sol: Optional[float],
    end_usdc: float,
    delta_usdc: Optional[float],
):
    # Title is display-only (balance). Wallet address is stored in WALLET_ADDR_PROP for querying.
    props = {
        "Date": {"date": {"start": date_iso}},
        TITLE_PROP_PERWALLET: {"title": [{"text": {"content": f"{round(end_sol, 2):.2f} SOL"}}]},
        WALLET_ADDR_PROP: {"rich_text": [{"text": {"content": wallet}}]},
        "End Balance": {"number": round(end_sol, 2)},
        "USDC End Balance": {"number": round(end_usdc, 2)},
    }

    if delta_sol is not None:
        props["Delta"] = {"number": round(delta_sol, 2)}
    if delta_usdc is not None:
        props["USDC Delta"] = {"number": round(delta_usdc, 2)}

    body = {"parent": {"database_id": db_id}, "properties": props}
    notion_req("https://api.notion.com/v1/pages", body)


def create_daily_total_row(
    db_id: str,
    date_iso: str,
    end_total_sol: float,
    delta_total_sol: Optional[float],
    end_total_usdc: float,
    delta_total_usdc: Optional[float],
):
    title_text = f"{round(end_total_sol, 2):.2f} SOL"
    props = {
        TOTAL_TITLE_PROP: {"title": [{"text": {"content": title_text}}]},
        "Date": {"date": {"start": date_iso}},
        "End Balance": {"number": round(end_total_sol, 2)},
        "USDC End Balance": {"number": round(end_total_usdc, 2)},
    }

    if delta_total_sol is not None:
        props["Delta"] = {"number": round(delta_total_sol, 2)}
    if delta_total_usdc is not None:
        props["USDC Delta"] = {"number": round(delta_total_usdc, 2)}

    body = {"parent": {"database_id": db_id}, "properties": props}
    notion_req("https://api.notion.com/v1/pages", body)


def main():
    if not NOTION_TOKEN:
        fail("NOTION_TOKEN missing")
    if not DB_PER:
        fail("NOTION_DB_PERWALLET missing")
    if not DB_TOTAL:
        fail("NOTION_DB_DAILYTOTAL missing")
    if not WALLETS:
        fail("WALLETS_CSV missing or empty")
    if not RPC_URL.startswith("https://"):
        fail(f"SOLANA_RPC_URL must be HTTPS; got '{RPC_URL}'")

    now_utc = datetime.now(timezone.utc)
    today_aest = now_utc.astimezone(AEST).date().isoformat()

    # 1) Sample all wallets (SOL + USDC)
    sampled = []
    for w in WALLETS:
        sol_bal = rpc_get_balance(w)
        usdc_bal = rpc_get_spl_mint_balance(w, USDC_MINT)
        sampled.append((w, sol_bal, usdc_bal))

    # 2) Compute per-wallet deltas vs latest prior Notion entry for that wallet
    per_rows = []
    for w, sol_current, usdc_current in sampled:
        prev_sol, prev_usdc = per_wallet_latest_numbers(DB_PER, w)

        delta_sol = None if prev_sol is None else (sol_current - prev_sol)
        delta_usdc = None if prev_usdc is None else (usdc_current - prev_usdc)

        per_rows.append((w, sol_current, delta_sol, usdc_current, delta_usdc))

    # 3) Write per-wallet rows for today
    for w, end_sol, delta_sol, end_usdc, delta_usdc in per_rows:
        create_per_wallet_row(DB_PER, today_aest, w, end_sol, delta_sol, end_usdc, delta_usdc)

    # 4) Compute & write daily totals (SOL + USDC)
    end_total_sol = sum(end_sol for _, end_sol, _, _, _ in per_rows)
    end_total_usdc = sum(end_usdc for _, _, _, end_usdc, _ in per_rows)

    prev_total_sol, prev_total_usdc = latest_total_numbers(DB_TOTAL)

    delta_total_sol = None if prev_total_sol is None else (end_total_sol - prev_total_sol)
    delta_total_usdc = None if prev_total_usdc is None else (end_total_usdc - prev_total_usdc)

    create_daily_total_row(DB_TOTAL, today_aest, end_total_sol, delta_total_sol, end_total_usdc, delta_total_usdc)

    # 5) Console log
    print(f"{today_aest} | {len(per_rows)} wallets | USDC mint={USDC_MINT}")
    for w, end_sol, delta_sol, end_usdc, delta_usdc in per_rows:
        dsol = "None" if delta_sol is None else f"{round(delta_sol, 2):+.2f}"
        dusdc = "None" if delta_usdc is None else f"{round(delta_usdc, 2):+.2f}"
        print(
            f"  {w[:8]}…  SOL End={round(end_sol, 2):.2f}  Δ={dsol} | "
            f"USDC End={round(end_usdc, 2):.2f}  Δ={dusdc}"
        )

    tsol = "None" if delta_total_sol is None else f"{round(delta_total_sol, 2):+.2f}"
    tusdc = "None" if delta_total_usdc is None else f"{round(delta_total_usdc, 2):+.2f}"
    print(
        f"TOTAL SOL End={round(end_total_sol, 2):.2f}  Δ={tsol} | "
        f"USDC End={round(end_total_usdc, 2):.2f}  Δ={tusdc}"
    )


if __name__ == "__main__":
    main()
