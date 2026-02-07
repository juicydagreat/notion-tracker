#!/usr/bin/env python3
"""
sol_multi_to_notion.py

Writes daily SOL + USDC balances into two Notion databases:
- Per Wallet DB: one row per wallet per day (includes deltas vs previous entry for that wallet)
- Daily Total DB: one row per day with aggregated totals (includes deltas vs previous day total)

Required GitHub Secrets (Actions):
- NOTION_TOKEN
- NOTION_DB_PERWALLET
- NOTION_DB_DAILYTOTAL
- WALLETS_CSV          (supports many formats; see parse_wallets_from_env)
Optional:
- SOLANA_RPC_URL       (defaults to https://api.mainnet-beta.solana.com)
"""

import os
import json
import time
import datetime
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional, Tuple


USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"  # mainnet USDC


# ----------------------------
# Utilities
# ----------------------------

def _now_utc_date_iso() -> str:
    # Notion "date" property accepts YYYY-MM-DD
    return datetime.datetime.utcnow().date().isoformat()


def _to_float(x: Any) -> float:
    try:
        if x is None:
            return 0.0
        return float(x)
    except Exception:
        return 0.0


def _is_probable_solana_address(s: str) -> bool:
    # Very lightweight sanity check (base58-ish length)
    s = s.strip()
    return 32 <= len(s) <= 60 and " " not in s and "\t" not in s and "\n" not in s


# ----------------------------
# Wallet parsing (fixes your error)
# ----------------------------

def parse_wallets_from_env(raw: str) -> List[str]:
    """
    Accepts:
    - CSV with header wallet:
        wallet
        addr1
        addr2
    - Newline list:
        addr1
        addr2
    - Comma list:
        addr1,addr2
    - JSON list:
        ["addr1","addr2"]
    - CSV with other header names (Wallet, address, wallet_address) -> also works
    """
    if raw is None:
        return []

    s = raw.strip()
    if not s:
        return []

    # JSON list support
    if s.startswith("[") and s.endswith("]"):
        try:
            arr = json.loads(s)
            if isinstance(arr, list):
                out = []
                for item in arr:
                    if isinstance(item, str) and _is_probable_solana_address(item):
                        out.append(item.strip())
                return list(dict.fromkeys(out))
        except Exception:
            pass

    # Normalize line endings
    s = s.replace("\r\n", "\n").replace("\r", "\n")

    # If it looks like CSV with header (first line has letters and maybe commas)
    lines = [ln.strip() for ln in s.split("\n") if ln.strip()]

    # If single line and contains commas, treat as comma-separated list
    if len(lines) == 1 and "," in lines[0]:
        parts = [p.strip() for p in lines[0].split(",") if p.strip()]
        out = [p for p in parts if _is_probable_solana_address(p)]
        return list(dict.fromkeys(out))

    # If multi-line, detect a header line
    header_candidates = {"wallet", "address", "wallet_address", "walletaddress", "addr"}
    first = lines[0].lower().replace(" ", "").replace("\t", "")
    if first in header_candidates or first.startswith("wallet") or first.startswith("address"):
        lines = lines[1:]  # drop header

    # Also handle 1-col CSV exported with commas (e.g., "wallet, ...")
    # If first line contains comma and header word
    if "," in (lines[0] if lines else ""):
        first_line = lines[0].lower()
        if "wallet" in first_line or "address" in first_line:
            # parse as CSV-ish: take first column values after header
            # rebuild by joining lines and splitting on commas/newlines
            blob = "\n".join(lines)
            # drop the header row by removing first line
            blob = "\n".join(lines[1:])
            blob = blob.replace(",", "\n")
            lines = [ln.strip() for ln in blob.split("\n") if ln.strip()]

    out = [ln for ln in lines if _is_probable_solana_address(ln)]
    # de-dupe preserving order
    return list(dict.fromkeys(out))


def load_wallets() -> List[str]:
    raw = os.environ.get("WALLETS_CSV", "")
    wallets = parse_wallets_from_env(raw)
    return wallets


# ----------------------------
# Solana RPC
# ----------------------------

def rpc_post(payload: Dict[str, Any]) -> Dict[str, Any]:
    url = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com").strip()
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})

    # Retry on 429 / transient network errors
    backoff = 1.0
    for attempt in range(7):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(backoff)
                backoff *= 2
                continue
            raise
        except Exception:
            time.sleep(backoff)
            backoff *= 2

    raise Exception("RPC failed after retries (possible rate limit or RPC outage).")


def rpc_get_sol_balance(wallet: str) -> float:
    payload = {"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [wallet]}
    out = rpc_post(payload)
    lamports = out.get("result", {}).get("value", 0)
    return float(lamports) / 1e9


def rpc_get_spl_mint_balance(wallet: str, mint: str) -> float:
    """
    Reliable: getTokenAccountsByOwner filtered by mint,
    sum uiAmount across all token accounts (some wallets can have more than one ATA).
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
    accounts = out.get("result", {}).get("value", [])
    total = 0.0
    for acc in accounts:
        try:
            info = acc["account"]["data"]["parsed"]["info"]
            token_amt = info["tokenAmount"]
            ui = token_amt.get("uiAmount")
            if ui is None:
                # fallback to amount + decimals
                amt = float(token_amt.get("amount", "0"))
                dec = int(token_amt.get("decimals", 0))
                ui = amt / (10 ** dec) if dec else amt
            total += float(ui)
        except Exception:
            continue
    return float(total)


# ----------------------------
# Notion API
# ----------------------------

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"


def notion_req(method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    token = os.environ.get("NOTION_TOKEN", "").strip()
    if not token:
        raise Exception("Missing NOTION_TOKEN")

    url = NOTION_API + path
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)

    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw)


def notion_get_database(db_id: str) -> Dict[str, Any]:
    return notion_req("GET", f"/databases/{db_id}")


def notion_query_database(db_id: str, filter_obj: Optional[Dict[str, Any]] = None, sorts: Optional[List[Dict[str, Any]]] = None, page_size: int = 10) -> Dict[str, Any]:
    body: Dict[str, Any] = {"page_size": page_size}
    if filter_obj is not None:
        body["filter"] = filter_obj
    if sorts is not None:
        body["sorts"] = sorts
    return notion_req("POST", f"/databases/{db_id}/query", body)


def notion_create_page(db_id: str, props: Dict[str, Any]) -> Dict[str, Any]:
    body = {
        "parent": {"database_id": db_id},
        "properties": props,
    }
    return notion_req("POST", "/pages", body)


def detect_title_prop_name(db_id: str) -> str:
    """
    Fixes your "Name is not a property" issue by auto-detecting the real title property.
    """
    db = notion_get_database(db_id)
    props = db.get("properties", {})
    for prop_name, meta in props.items():
        if meta.get("type") == "title":
            return prop_name
    # extremely unusual, but fallback
    return "Name"


def pick_existing_prop(db_props: Dict[str, Any], candidates: List[str]) -> Optional[str]:
    """
    Case-insensitive lookup of a property name in a Notion database schema.
    Returns the actual property name if found.
    """
    lower_map = {k.lower(): k for k in db_props.keys()}
    for c in candidates:
        key = lower_map.get(c.lower())
        if key:
            return key
    return None


# ----------------------------
# Notion property builders
# ----------------------------

def prop_title(text: str) -> Dict[str, Any]:
    return {"title": [{"text": {"content": text}}]}


def prop_rich_text(text: str) -> Dict[str, Any]:
    return {"rich_text": [{"text": {"content": text}}]}


def prop_number(x: float) -> Dict[str, Any]:
    return {"number": float(x)}


def prop_date(date_iso: str) -> Dict[str, Any]:
    return {"date": {"start": date_iso}}


# ----------------------------
# Main logic
# ----------------------------

def get_prev_wallet_entry(perwallet_db: str, wallet_addr_prop: str, wallet: str, date_prop: str) -> Optional[Dict[str, Any]]:
    """
    Get the most recent previous entry for a specific wallet (any date).
    """
    filt = {
        "property": wallet_addr_prop,
        "rich_text": {"equals": wallet},
    }
    sorts = [{"property": date_prop, "direction": "descending"}]
    res = notion_query_database(perwallet_db, filter_obj=filt, sorts=sorts, page_size=1)
    results = res.get("results", [])
    return results[0] if results else None


def get_prev_total_entry(dailytotal_db: str, date_prop: str) -> Optional[Dict[str, Any]]:
    sorts = [{"property": date_prop, "direction": "descending"}]
    res = notion_query_database(dailytotal_db, filter_obj=None, sorts=sorts, page_size=1)
    results = res.get("results", [])
    return results[0] if results else None


def read_number_prop(page: Dict[str, Any], prop_name: str) -> float:
    try:
        p = page["properties"][prop_name]
        if p.get("type") == "number":
            return _to_float(p.get("number"))
    except Exception:
        pass
    return 0.0


def main() -> None:
    perwallet_db = os.environ.get("NOTION_DB_PERWALLET", "").strip()
    dailytotal_db = os.environ.get("NOTION_DB_DAILYTOTAL", "").strip()

    if not perwallet_db:
        raise Exception("Missing NOTION_DB_PERWALLET")
    if not dailytotal_db:
        raise Exception("Missing NOTION_DB_DAILYTOTAL")

    wallets = load_wallets()
    if not wallets:
        raise Exception("No wallets found in WALLETS_CSV (we tried CSV/newlines/commas/JSON).")

    # Fetch DB schemas so we can map property names reliably
    per_db_obj = notion_get_database(perwallet_db)
    per_props = per_db_obj.get("properties", {})
    total_db_obj = notion_get_database(dailytotal_db)
    total_props = total_db_obj.get("properties", {})

    # Auto-detect title properties (fixes "Name not a property")
    per_title_prop = detect_title_prop_name(perwallet_db)
    total_title_prop = detect_title_prop_name(dailytotal_db)

    # Map core columns (case-insensitive)
    per_date_prop = pick_existing_prop(per_props, ["Date"]) or "Date"
    per_end_prop = pick_existing_prop(per_props, ["End Balance", "EndBalance"]) or "End Balance"
    per_delta_prop = pick_existing_prop(per_props, ["Delta"]) or "Delta"
    per_wallet_addr_prop = pick_existing_prop(per_props, ["Wallet Address", "WalletAddress", "Address"]) or "Wallet Address"
    per_usdc_end_prop = pick_existing_prop(per_props, ["USDC End Balance", "USDC EndBalance"]) or "USDC End Balance"
    per_usdc_delta_prop = pick_existing_prop(per_props, ["USDC Delta", "USDCDelta"]) or "USDC Delta"

    total_date_prop = pick_existing_prop(total_props, ["Date"]) or "Date"
    total_end_prop = pick_existing_prop(total_props, ["End Balance", "EndBalance"]) or "End Balance"
    total_delta_prop = pick_existing_prop(total_props, ["Delta"]) or "Delta"
    total_usdc_end_prop = pick_existing_prop(total_props, ["USDC End Balance", "USDC EndBalance"]) or "USDC End Balance"
    total_usdc_delta_prop = pick_existing_prop(total_props, ["USDC Delta", "USDCDelta"]) or "USDC Delta"

    today = _now_utc_date_iso()

    # Pull balances
    per_rows: List[Tuple[str, float, float]] = []  # (wallet, sol, usdc)
    for w in wallets:
        sol_bal = rpc_get_sol_balance(w)
        usdc_bal = rpc_get_spl_mint_balance(w, USDC_MINT)
        per_rows.append((w, sol_bal, usdc_bal))

    # Write per-wallet rows
    total_sol = 0.0
    total_usdc = 0.0

    for (w, sol_bal, usdc_bal) in per_rows:
        total_sol += sol_bal
        total_usdc += usdc_bal

        prev = get_prev_wallet_entry(perwallet_db, per_wallet_addr_prop, w, per_date_prop)
        prev_sol = read_number_prop(prev, per_end_prop) if prev else 0.0
        prev_usdc = read_number_prop(prev, per_usdc_end_prop) if prev else 0.0

        sol_delta = sol_bal - prev_sol
        usdc_delta = usdc_bal - prev_usdc

        title_text = w  # show wallet in title for per-wallet DB

        props = {
            per_title_prop: prop_title(title_text),
            per_date_prop: prop_date(today),
            per_end_prop: prop_number(sol_bal),
            per_delta_prop: prop_number(sol_delta),
            per_wallet_addr_prop: prop_rich_text(w),
            per_usdc_end_prop: prop_number(usdc_bal),
            per_usdc_delta_prop: prop_number(usdc_delta),
        }

        notion_create_page(perwallet_db, props)

    # Write daily total row
    prev_total = get_prev_total_entry(dailytotal_db, total_date_prop)
    prev_total_sol = read_number_prop(prev_total, total_end_prop) if prev_total else 0.0
    prev_total_usdc = read_number_prop(prev_total, total_usdc_end_prop) if prev_total else 0.0

    total_delta = total_sol - prev_total_sol
    total_usdc_delta = total_usdc - prev_total_usdc

    total_title_text = f"{total_sol:.2f} SOL"

    total_page_props = {
        total_title_prop: prop_title(total_title_text),
        total_date_prop: prop_date(today),
        total_end_prop: prop_number(total_sol),
        total_delta_prop: prop_number(total_delta),
        total_usdc_end_prop: prop_number(total_usdc),
        total_usdc_delta_prop: prop_number(total_usdc_delta),
    }

    notion_create_page(dailytotal_db, total_page_props)

    print(f"OK: wrote {len(wallets)} per-wallet rows + 1 total row for {today}")


if __name__ == "__main__":
    main()
