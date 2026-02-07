# sol_multi_to_notion.py
# Daily run (schedule via GitHub Actions). Computes SOL delta and USDC delta separately.
#
# Required GitHub Secrets (Actions -> Secrets and variables):
#   NOTION_TOKEN            (Internal Integration token, starts with secret_)
#   NOTION_DB_PERWALLET     (DB id for "Per Wallet")
#   NOTION_DB_DAILYTOTAL    (DB id for "Daily Total")
#   WALLETS_CSV             (comma-separated Solana wallet addresses)
#
# Optional:
#   SOLANA_RPC_URL          (default https://api.mainnet-beta.solana.com)
#   TITLE_PROP_PERWALLET    (default "Name")          # Title column name in Per-Wallet DB
#   TOTAL_TITLE_PROP        (default "Name")          # Title column name in Daily Total DB
#   WALLET_ADDR_PROP        (default "Wallet Address")# Rich-text column to store wallet address (Per-Wallet DB)
#   USDC_MINT               (default mainnet USDC mint EPjFWdd5...TDt1v)
#
# Expected Notion schema
# Per-Wallet DB:
#   - Title: TITLE_PROP_PERWALLET (we write "{sol} SOL" to this title)
#   - WALLET_ADDR_PROP (Rich text): actual wallet address (used for querying previous entries)
#   - Date (Date)
#   - End Balance (Number)         # SOL end
#   - Delta (Number)               # SOL delta
#   - USDC End Balance (Number)    # USDC end
#   - USDC Delta (Number)          # USDC delta
#
# Daily-Total DB:
#   - Title: TOTAL_TITLE_PROP (we write "{total_sol} SOL" here)
#   - Date (Date)
#   - End Balance (Number)         # total SOL end
#   - Delta (Number)               # total SOL delta
#   (USDC totals are not written here in this version—easy to add later if you want.)

import os, sys, json, time, urllib.request, urllib.error
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Optional, Tuple, Any, Dict, List

RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com").strip()
NOTION_TOKEN = os.getenv("NOTION_TOKEN", "").strip()
DB_PER = os.getenv("NOTION_DB_PERWALLET", "").strip()
DB_TOTAL = os.getenv("NOTION_DB_DAILYTOTAL", "").strip()
WALLETS = [w.strip() for w in (os.getenv("WALLETS_CSV") or "").split(",") if w.strip()]

TITLE_PROP_PERWALLET = os.getenv("TITLE_PROP_PERWALLET", "Name").strip()
TOTAL_TITLE_PROP = os.getenv("TOTAL_TITLE_PROP", "Name").strip()
WALLET_ADDR_PROP = os.getenv("WALLET_ADDR_PROP", "Wallet Address").strip()

# Mainnet USDC mint
USDC_MINT = os.getenv("USDC_MINT", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v").strip()

AEST = ZoneInfo("Australia/Brisbane")


def fail(msg: str):
    print(f"ERROR: {msg}")
    sys.exit(1)


# -------------------------
# Notion helpers
# -------------------------
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


def safe_get_number(props: dict, key: str) -> Optional[float]:
    v = props.get(key, {})
    if isinstance(v, dict) and "number" in v:
        return v.get("number")
    return None


def safe_get_date(props: dict, key: str) -> Optional[str]:
    v = props.get(key, {})
    if isinstance(v, dict):
        d = v.get("date", {})
        if isinstance(d, dict):
            return d.get("start")
    return None


# -------------------------
# Solana RPC helpers (with retry/backoff)
# -------------------------
def rpc_post(payload: dict, max_retries: int = 6) -> dict:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(RPC_URL, data=data, headers={"Content-Type": "application/json"})
    last_err = None

    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            last_err = f"HTTP {e.code}: {body}"

            # 429 rate-limit: exponential backoff
            if e.code == 429:
                sleep_s = min(12.0, 0.75 * (2 ** attempt))
                print(f"RPC 429 rate-limited. Sleeping {sleep_s:.2f}s then retrying...")
                time.sleep(sleep_s)
                continue

            # Other HTTP errors: fail fast
            fail(f"RPC error: {last_err}")
        except Exception as e:
            last_err = str(e)
            sleep_s = min(12.0, 0.5 * (2 ** attempt))
            print(f"RPC error: {last_err}. Sleeping {sleep_s:.2f}s then retrying...")
            time.sleep(sleep_s)

    fail(f"RPC failed after retries: {last_err}")


def rpc_get_sol_balance(wallet: str) -> float:
    payload = {"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [wallet, {"commitment": "finalized"}]}
    out = rpc_post(payload)
    try:
        lamports = out["result"]["value"]
    except Exception:
        fail(f"Unexpected getBalance response for {wallet}: {out}")
    return lamports / 1_000_000_000


def rpc_get_usdc_balance(wallet: str) -> float:
    """
    Robust USDC balance:
    - Query token accounts by owner, filtered by USDC mint
    - Parse jsonParsed balances
    - Sum across all USDC token accounts
    """
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTokenAccountsByOwner",
        "params": [
            wallet,
            {"mint": USDC_MINT},
            {"encoding": "jsonParsed", "commitment": "finalized"}
        ]
    }
    out = rpc_post(payload)

    try:
        accounts = out["result"]["value"]
    except Exception:
        fail(f"Unexpected getTokenAccountsByOwner response for {wallet}: {out}")

    total = 0.0
    for acc in accounts:
        try:
            info = acc["account"]["data"]["parsed"]["info"]
            token_amount = info["tokenAmount"]
            # Prefer uiAmountString for precision
            ui_amount_str = token_amount.get("uiAmountString")
            if ui_amount_str is not None:
                total += float(ui_amount_str)
            else:
                ui_amount = token_amount.get("uiAmount")
                if ui_amount is not None:
                    total += float(ui_amount)
        except Exception:
            # If one account is malformed, skip it rather than blowing up.
            continue

    return float(total)


# -------------------------
# Notion query helpers
# -------------------------
def per_wallet_latest_row(db_id: str, wallet_addr: str) -> Optional[dict]:
    """
    Find latest Per-Wallet row by wallet address stored in WALLET_ADDR_PROP (rich_text).
    """
    body = {
        "filter": {"property": WALLET_ADDR_PROP, "rich_text": {"equals": wallet_addr}},
        "sorts": [{"property": "Date", "direction": "descending"}],
        "page_size": 1
    }
    res = notion_req(f"https://api.notion.com/v1/databases/{db_id}/query", body)
    results = res.get("results", [])
    return results[0] if results else None


def latest_total_end_and_date(db_id: str) -> Tuple[Optional[float], Optional[str]]:
    body = {"sorts": [{"property": "Date", "direction": "descending"}], "page_size": 1}
    res = notion_req(f"https://api.notion.com/v1/databases/{db_id}/query", body)
    results = res.get("results", [])
    if not results:
        return None, None
    props = results[0]["properties"]
    end_prop = safe_get_number(props, "End Balance")
    date_prop = safe_get_date(props, "Date")
    return end_prop, date_prop


# -------------------------
# Notion create row helpers
# -------------------------
def create_per_wallet_row(
    db_id: str,
    date_iso: str,
    wallet_addr: str,
    sol_end: float,
    sol_delta: Optional[float],
    usdc_end: float,
    usdc_delta: Optional[float],
):
    title_text = f"{sol_end:.2f} SOL"

    props: Dict[str, Any] = {
        "Date": {"date": {"start": date_iso}},
        TITLE_PROP_PERWALLET: {"title": [{"text": {"content": title_text}}]},
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


def create_daily_total_row(db_id: str, date_iso: str, end_total_sol: float, delta_total_sol: Optional[float]):
    title_text = f"{end_total_sol:.2f} SOL"
    props: Dict[str, Any] = {
        TOTAL_TITLE_PROP: {"title": [{"text": {"content": title_text}}]},
        "Date": {"date": {"start": date_iso}},
        "End Balance": {"number": round(end_total_sol, 2)},
    }
    if delta_total_sol is not None:
        props["Delta"] = {"number": round(delta_total_sol, 2)}

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

    per_rows = []

    # Light pacing to reduce rate limiting on public RPC
    for w in WALLETS:
        sol_end = rpc_get_sol_balance(w)
        time.sleep(0.2)
        usdc_end = rpc_get_usdc_balance(w)
        time.sleep(0.2)

        prev = per_wallet_latest_row(DB_PER, w)
        sol_prev = None
        usdc_prev = None
        if prev is not None:
            props = prev.get("properties", {})
            sol_prev = safe_get_number(props, "End Balance")
            usdc_prev = safe_get_number(props, "USDC End Balance")

        sol_delta = None if sol_prev is None else (sol_end - sol_prev)
        usdc_delta = None if usdc_prev is None else (usdc_end - usdc_prev)

        per_rows.append((w, sol_end, sol_delta, usdc_end, usdc_delta))

    # Write per-wallet rows
    for w, sol_end, sol_delta, usdc_end, usdc_delta in per_rows:
        create_per_wallet_row(DB_PER, today_aest, w, sol_end, sol_delta, usdc_end, usdc_delta)

    # Daily total (SOL only)
    end_total_sol = sum(r[1] for r in per_rows)
    prev_total_end, _ = latest_total_end_and_date(DB_TOTAL)
    delta_total_sol = None if prev_total_end is None else (end_total_sol - prev_total_end)
    create_daily_total_row(DB_TOTAL, today_aest, end_total_sol, delta_total_sol)

    # Console log
    print(f"{today_aest} | {len(per_rows)} wallets")
    for w, sol_end, sol_delta, usdc_end, usdc_delta in per_rows:
        sd = "None" if sol_delta is None else f"{sol_delta:+.2f}"
        ud = "None" if usdc_delta is None else f"{usdc_delta:+.2f}"
        print(f"  {w[:8]}… SOL={sol_end:.2f} Δ={sd} | USDC={usdc_end:.2f} Δ={ud}")
    td = "None" if delta_total_sol is None else f"{delta_total_sol:+.2f}"
    print(f"TOTAL SOL End={end_total_sol:.2f}  Δ={td}")


if __name__ == "__main__":
    main()
