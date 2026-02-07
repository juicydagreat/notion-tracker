# usdc_sol_multi_to_notion.py
# Daily run (schedule via GitHub Actions). Writes SOL + USDC balances + deltas into Notion.
#
# Required GitHub Secrets:
#   NOTION_TOKEN
#   NOTION_DB_PERWALLET
#   NOTION_DB_DAILYTOTAL
#   WALLETS_CSV                  (comma-separated Solana wallet addresses)
#
# Recommended Secret:
#   SOLANA_RPC_URL               (your private RPC to avoid 429s)
#
# Optional env overrides (only needed if your Notion column names differ):
#   SOLANA_RPC_URL               (default https://api.mainnet-beta.solana.com)
#   TITLE_PROP_PERWALLET         (default "Name")            # Notion Title column in Per-Wallet DB
#   WALLET_ADDR_PROP             (default "Wallet Address")  # Rich text column for the actual wallet address
#   TOTAL_TITLE_PROP             (default "Name")            # Notion Title column in Daily Total DB
#   USDC_MINT                    (default mainnet USDC mint)
#
# Expected Notion schema
# Per-Wallet DB columns:
#   - Title column: "Name" (default)   -> we write "{sol:.2f} SOL" for display
#   - Wallet Address (Rich text)       -> stores the actual wallet address (used for querying history)
#   - Date (Date)
#   - End Balance (Number)             -> SOL end
#   - Delta (Number)                   -> SOL delta
#   - USDC End Balance (Number)        -> USDC end
#   - USDC Delta (Number)              -> USDC delta
#
# Daily Total DB columns:
#   - Title column: "Name" (default)   -> we write "{total_sol:.2f} SOL" for calendar cards
#   - Date (Date)
#   - End Balance (Number)             -> total SOL end
#   - Delta (Number)                   -> total SOL delta
#   - USDC End Balance (Number)        -> total USDC end
#   - USDC Delta (Number)              -> total USDC delta

import os
import sys
import json
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Optional, Tuple, Dict, Any, List


# -----------------------------
# Config / env
# -----------------------------
RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com").strip()
NOTION_TOKEN = os.getenv("NOTION_TOKEN", "").strip()
DB_PER = os.getenv("NOTION_DB_PERWALLET", "").strip()
DB_TOTAL = os.getenv("NOTION_DB_DAILYTOTAL", "").strip()
WALLETS = [w.strip() for w in (os.getenv("WALLETS_CSV") or "").split(",") if w.strip()]

# Notion column names (defaults chosen to match your screenshots: Title is "Name")
TITLE_PROP_PERWALLET = os.getenv("TITLE_PROP_PERWALLET", "Name").strip()
WALLET_ADDR_PROP = os.getenv("WALLET_ADDR_PROP", "Wallet Address").strip()
TOTAL_TITLE_PROP = os.getenv("TOTAL_TITLE_PROP", "Name").strip()

# Mainnet USDC mint (canonical)
USDC_MINT = os.getenv("USDC_MINT", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v").strip()

AEST = ZoneInfo("Australia/Brisbane")


# -----------------------------
# Helpers
# -----------------------------
def fail(msg: str) -> None:
    print(f"ERROR: {msg}")
    sys.exit(1)


def _http_read_json(req: urllib.request.Request, timeout: int = 30) -> Dict[str, Any]:
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode() or "{}"
        return json.loads(raw)


def notion_req(url: str, body: Optional[dict] = None, method: str = "POST") -> dict:
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
        return _http_read_json(req, timeout=30)
    except urllib.error.HTTPError as e:
        detail = e.read().decode()
        fail(f"Notion {e.code} {e.reason}: {detail}")


def rpc_post(payload: dict, max_retries: int = 6) -> dict:
    """
    RPC POST with exponential backoff for 429s and transient errors.
    """
    if not RPC_URL.startswith("https://"):
        fail(f"SOLANA_RPC_URL must be HTTPS; got '{RPC_URL}'")

    data = json.dumps(payload).encode()
    req = urllib.request.Request(RPC_URL, data=data, headers={"Content-Type": "application/json"})

    backoff = 1.0
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            return _http_read_json(req, timeout=30)
        except urllib.error.HTTPError as e:
            last_err = e
            # 429 Too Many Requests -> backoff + retry
            if e.code == 429:
                time.sleep(backoff)
                backoff = min(backoff * 2.0, 16.0)
                continue
            # other HTTP errors: surface immediately
            detail = e.read().decode()
            fail(f"RPC {e.code} {e.reason}: {detail}")
        except Exception as e:
            last_err = e
            time.sleep(backoff)
            backoff = min(backoff * 2.0, 16.0)

    fail(f"RPC failed after {max_retries} retries: {last_err}")


# -----------------------------
# Solana balances
# -----------------------------
def rpc_get_sol_balance(wallet: str) -> float:
    payload = {"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [wallet, {"commitment": "finalized"}]}
    out = rpc_post(payload)
    try:
        lamports = out["result"]["value"]
    except Exception:
        fail(f"Unexpected RPC response for SOL balance {wallet}: {out}")
    return lamports / 1_000_000_000


def rpc_get_spl_mint_balance_primary(wallet: str, mint: str) -> float:
    """
    IMPORTANT: Do NOT sum all token accounts (this often overcounts and won't match Ledger Live).
    Instead, pick the single "primary" token account by taking the token account with the largest uiAmount.

    This matches Ledger Live in practice because Ledger generally displays the main ATA balance.
    """
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTokenAccountsByOwner",
        "params": [
            wallet,
            {"mint": mint},
            {"encoding": "jsonParsed"},
        ],
    }
    out = rpc_post(payload)

    value = (out.get("result") or {}).get("value") or []
    if not value:
        return 0.0

    best = 0.0
    for item in value:
        try:
            info = item["account"]["data"]["parsed"]["info"]
            amt = info["tokenAmount"]
            ui = amt.get("uiAmount")
            if ui is None:
                ui = float(amt.get("uiAmountString", "0") or "0")
            ui = float(ui)
            if ui > best:
                best = ui
        except Exception:
            continue

    return float(best)


# -----------------------------
# Notion reads (for delta)
# -----------------------------
def _rich_text_equals(prop_name: str, value: str) -> dict:
    return {"property": prop_name, "rich_text": {"equals": value}}


def per_wallet_latest_row(db_id: str, wallet_addr: str) -> Optional[dict]:
    """
    Fetch most recent row for this wallet by querying the Wallet Address rich-text column.
    """
    body = {
        "filter": _rich_text_equals(WALLET_ADDR_PROP, wallet_addr),
        "sorts": [{"property": "Date", "direction": "descending"}],
        "page_size": 1,
    }
    res = notion_req(f"https://api.notion.com/v1/databases/{db_id}/query", body)
    results = res.get("results", [])
    if not results:
        return None
    return results[0]


def latest_total_row(db_id: str) -> Optional[dict]:
    body = {"sorts": [{"property": "Date", "direction": "descending"}], "page_size": 1}
    res = notion_req(f"https://api.notion.com/v1/databases/{db_id}/query", body)
    results = res.get("results", [])
    if not results:
        return None
    return results[0]


def _num_prop(props: dict, name: str) -> Optional[float]:
    v = props.get(name, {}).get("number")
    return None if v is None else float(v)


# -----------------------------
# Notion writes
# -----------------------------
def create_per_wallet_row(
    db_id: str,
    date_iso: str,
    wallet_addr: str,
    sol_end: float,
    sol_delta: Optional[float],
    usdc_end: float,
    usdc_delta: Optional[float],
) -> None:
    props: Dict[str, Any] = {
        "Date": {"date": {"start": date_iso}},
        # Title shows SOL balance for quick scanning
        TITLE_PROP_PERWALLET: {"title": [{"text": {"content": f"{sol_end:.2f} SOL"}}]},
        WALLET_ADDR_PROP: {"rich_text": [{"text": {"content": wallet_addr}}]},
        "End Balance": {"number": round(sol_end, 2)},
        "USDC End Balance": {"number": round(usdc_end, 2)},
    }

    if sol_delta is not None:
        props["Delta"] = {"number": round(sol_delta, 2)}
    if usdc_delta is not None:
        props["USDC Delta"] = {"number": round(usdc_delta, 2)}

    body = {"parent": {"database_id": db_id}, "properties": props}
    notion_req("https://api.notion.com/v1/pages", body)


def create_daily_total_row(
    db_id: str,
    date_iso: str,
    sol_total_end: float,
    sol_total_delta: Optional[float],
    usdc_total_end: float,
    usdc_total_delta: Optional[float],
) -> None:
    title_text = f"{sol_total_end:.2f} SOL"
    props: Dict[str, Any] = {
        TOTAL_TITLE_PROP: {"title": [{"text": {"content": title_text}}]},
        "Date": {"date": {"start": date_iso}},
        "End Balance": {"number": round(sol_total_end, 2)},
        "USDC End Balance": {"number": round(usdc_total_end, 2)},
    }

    if sol_total_delta is not None:
        props["Delta"] = {"number": round(sol_total_delta, 2)}
    if usdc_total_delta is not None:
        props["USDC Delta"] = {"number": round(usdc_total_delta, 2)}

    body = {"parent": {"database_id": db_id}, "properties": props}
    notion_req("https://api.notion.com/v1/pages", body)


# -----------------------------
# Main
# -----------------------------
def main() -> None:
    if not NOTION_TOKEN:
        fail("NOTION_TOKEN missing")
    if not DB_PER:
        fail("NOTION_DB_PERWALLET missing")
    if not DB_TOTAL:
        fail("NOTION_DB_DAILYTOTAL missing")
    if not WALLETS:
        fail("WALLETS_CSV missing or empty")

    now_utc = datetime.now(timezone.utc)
    today_aest = now_utc.astimezone(AEST).date().isoformat()

    # 1) Sample all wallets (SOL + USDC)
    rows: List[Dict[str, Any]] = []
    for w in WALLETS:
        sol = rpc_get_sol_balance(w)
        usdc = rpc_get_spl_mint_balance_primary(w, USDC_MINT)
        rows.append({"wallet": w, "sol": sol, "usdc": usdc})

    # 2) Compute per-wallet deltas vs last recorded row for that wallet
    for r in rows:
        prev = per_wallet_latest_row(DB_PER, r["wallet"])
        if prev is None:
            r["sol_delta"] = None
            r["usdc_delta"] = None
        else:
            props = prev.get("properties", {})
            prev_sol = _num_prop(props, "End Balance")
            prev_usdc = _num_prop(props, "USDC End Balance")

            r["sol_delta"] = None if prev_sol is None else (r["sol"] - prev_sol)
            r["usdc_delta"] = None if prev_usdc is None else (r["usdc"] - prev_usdc)

    # 3) Write per-wallet rows
    for r in rows:
        create_per_wallet_row(
            DB_PER,
            today_aest,
            r["wallet"],
            r["sol"],
            r["sol_delta"],
            r["usdc"],
            r["usdc_delta"],
        )

    # 4) Daily totals
    sol_total_end = sum(float(r["sol"]) for r in rows)
    usdc_total_end = sum(float(r["usdc"]) for r in rows)

    prev_total = latest_total_row(DB_TOTAL)
    if prev_total is None:
        sol_total_delta = None
        usdc_total_delta = None
    else:
        props = prev_total.get("properties", {})
        prev_sol_total = _num_prop(props, "End Balance")
        prev_usdc_total = _num_prop(props, "USDC End Balance")

        sol_total_delta = None if prev_sol_total is None else (sol_total_end - prev_sol_total)
        usdc_total_delta = None if prev_usdc_total is None else (usdc_total_end - prev_usdc_total)

    create_daily_total_row(
        DB_TOTAL,
        today_aest,
        sol_total_end,
        sol_total_delta,
        usdc_total_end,
        usdc_total_delta,
    )

    # 5) Console log
    print(f"{today_aest} | {len(rows)} wallets | RPC={RPC_URL}")
    for r in rows:
        sd = "None" if r["sol_delta"] is None else f"{r['sol_delta']:+.2f}"
        ud = "None" if r["usdc_delta"] is None else f"{r['usdc_delta']:+.2f}"
        print(f"  {r['wallet'][:8]}…  SOL={r['sol']:.2f}  Δ={sd} | USDC={r['usdc']:.2f}  Δ={ud}")
    t_sd = "None" if sol_total_delta is None else f"{sol_total_delta:+.2f}"
    t_ud = "None" if usdc_total_delta is None else f"{usdc_total_delta:+.2f}"
    print(f"TOTAL  SOL={sol_total_end:.2f}  Δ={t_sd} | USDC={usdc_total_end:.2f}  Δ={t_ud}")


if __name__ == "__main__":
    main()
