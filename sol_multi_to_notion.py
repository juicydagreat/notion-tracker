#!/usr/bin/env python3
import os
import json
import time
import datetime
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional, Tuple

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"


# ----------------------------
# Helpers
# ----------------------------

def _now_utc_date_iso() -> str:
    return datetime.datetime.utcnow().date().isoformat()


def _is_probable_solana_address(s: str) -> bool:
    s = s.strip()
    return 32 <= len(s) <= 60 and " " not in s and "\t" not in s and "\n" not in s


def parse_wallets_from_env(raw: str) -> List[str]:
    if raw is None:
        return []
    s = raw.strip()
    if not s:
        return []

    # JSON list
    if s.startswith("[") and s.endswith("]"):
        try:
            arr = json.loads(s)
            if isinstance(arr, list):
                out = [x.strip() for x in arr if isinstance(x, str) and _is_probable_solana_address(x)]
                return list(dict.fromkeys(out))
        except Exception:
            pass

    s = s.replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln.strip() for ln in s.split("\n") if ln.strip()]

    # Comma list in single line
    if len(lines) == 1 and "," in lines[0]:
        parts = [p.strip() for p in lines[0].split(",") if p.strip()]
        out = [p for p in parts if _is_probable_solana_address(p)]
        return list(dict.fromkeys(out))

    # Header detection
    header_candidates = {"wallet", "address", "wallet_address", "walletaddress", "addr"}
    first = lines[0].lower().replace(" ", "").replace("\t", "")
    if first in header_candidates or first.startswith("wallet") or first.startswith("address"):
        lines = lines[1:]

    out = [ln for ln in lines if _is_probable_solana_address(ln)]
    return list(dict.fromkeys(out))


def load_wallets() -> List[str]:
    return parse_wallets_from_env(os.environ.get("WALLETS_CSV", ""))


# ----------------------------
# Solana RPC (FAIL LOUD)
# ----------------------------

def rpc_url() -> str:
    return os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com").strip()


def rpc_post(payload: Dict[str, Any]) -> Dict[str, Any]:
    url = rpc_url()
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})

    backoff = 1.0
    last_err: Optional[Exception] = None

    for attempt in range(8):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                out = json.loads(raw)

            # IMPORTANT: do not allow silent zeros
            if isinstance(out, dict) and "error" in out and out["error"]:
                raise Exception(f"Solana RPC error: {out['error']}")

            return out

        except urllib.error.HTTPError as e:
            # Read body if present (useful for debugging)
            body = ""
            try:
                body = e.read().decode("utf-8")
            except Exception:
                pass

            if e.code == 429:
                last_err = Exception(f"HTTP 429 Too Many Requests. Body: {body[:500]}")
                time.sleep(backoff)
                backoff *= 2
                continue

            raise Exception(f"HTTP {e.code} from RPC. Body: {body[:500]}")

        except Exception as e:
            last_err = e
            time.sleep(backoff)
            backoff *= 2

    raise Exception(f"RPC failed after retries. Last error: {last_err}")


def rpc_get_sol_balance(wallet: str) -> float:
    payload = {"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [wallet]}
    out = rpc_post(payload)
    lamports = out["result"]["value"]
    return float(lamports) / 1e9


def rpc_get_spl_mint_balance(wallet: str, mint: str) -> float:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTokenAccountsByOwner",
        "params": [wallet, {"mint": mint}, {"encoding": "jsonParsed"}],
    }
    out = rpc_post(payload)
    accounts = out["result"]["value"]

    total = 0.0
    for acc in accounts:
        info = acc["account"]["data"]["parsed"]["info"]
        token_amt = info["tokenAmount"]
        ui = token_amt.get("uiAmount")
        if ui is None:
            amt = float(token_amt.get("amount", "0"))
            dec = int(token_amt.get("decimals", 0))
            ui = amt / (10 ** dec) if dec else amt
        total += float(ui)

    return total


# ----------------------------
# Notion
# ----------------------------

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


def notion_query_database(db_id: str, filter_obj: Optional[Dict[str, Any]] = None,
                          sorts: Optional[List[Dict[str, Any]]] = None, page_size: int = 10) -> Dict[str, Any]:
    body: Dict[str, Any] = {"page_size": page_size}
    if filter_obj is not None:
        body["filter"] = filter_obj
    if sorts is not None:
        body["sorts"] = sorts
    return notion_req("POST", f"/databases/{db_id}/query", body)


def notion_create_page(db_id: str, props: Dict[str, Any]) -> Dict[str, Any]:
    body = {"parent": {"database_id": db_id}, "properties": props}
    return notion_req("POST", "/pages", body)


def detect_title_prop_name(db_id: str) -> str:
    db = notion_get_database(db_id)
    props = db.get("properties", {})
    for prop_name, meta in props.items():
        if meta.get("type") == "title":
            return prop_name
    return "Name"


def pick_existing_prop(db_props: Dict[str, Any], candidates: List[str]) -> Optional[str]:
    lower_map = {k.lower(): k for k in db_props.keys()}
    for c in candidates:
        key = lower_map.get(c.lower())
        if key:
            return key
    return None


def prop_title(text: str) -> Dict[str, Any]:
    return {"title": [{"text": {"content": text}}]}


def prop_rich_text(text: str) -> Dict[str, Any]:
    return {"rich_text": [{"text": {"content": text}}]}


def prop_number(x: float) -> Dict[str, Any]:
    return {"number": float(x)}


def prop_date(date_iso: str) -> Dict[str, Any]:
    return {"date": {"start": date_iso}}


def read_number_prop(page: Optional[Dict[str, Any]], prop_name: str) -> float:
    if not page:
        return 0.0
    try:
        p = page["properties"][prop_name]
        if p.get("type") == "number":
            v = p.get("number")
            return float(v) if v is not None else 0.0
    except Exception:
        pass
    return 0.0


def get_prev_wallet_entry(perwallet_db: str, wallet_addr_prop: str, wallet: str, date_prop: str) -> Optional[Dict[str, Any]]:
    filt = {"property": wallet_addr_prop, "rich_text": {"equals": wallet}}
    sorts = [{"property": date_prop, "direction": "descending"}]
    res = notion_query_database(perwallet_db, filter_obj=filt, sorts=sorts, page_size=1)
    results = res.get("results", [])
    return results[0] if results else None


def get_prev_total_entry(dailytotal_db: str, date_prop: str) -> Optional[Dict[str, Any]]:
    sorts = [{"property": date_prop, "direction": "descending"}]
    res = notion_query_database(dailytotal_db, filter_obj=None, sorts=sorts, page_size=1)
    results = res.get("results", [])
    return results[0] if results else None


# ----------------------------
# Main
# ----------------------------

def main() -> None:
    perwallet_db = os.environ.get("NOTION_DB_PERWALLET", "").strip()
    dailytotal_db = os.environ.get("NOTION_DB_DAILYTOTAL", "").strip()
    if not perwallet_db:
        raise Exception("Missing NOTION_DB_PERWALLET")
    if not dailytotal_db:
        raise Exception("Missing NOTION_DB_DAILYTOTAL")

    wallets = load_wallets()
    if not wallets:
        raise Exception("No wallets found in WALLETS_CSV (parsed nothing).")

    # Print debug so you can see exactly what is being used in Actions logs
    print(f"RPC URL: {rpc_url()}")
    print(f"Wallet count: {len(wallets)}")
    print(f"First wallet: {wallets[0]}")

    per_db_obj = notion_get_database(perwallet_db)
    per_props = per_db_obj.get("properties", {})
    total_db_obj = notion_get_database(dailytotal_db)
    total_props = total_db_obj.get("properties", {})

    per_title_prop = detect_title_prop_name(perwallet_db)
    total_title_prop = detect_title_prop_name(dailytotal_db)

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

    per_rows: List[Tuple[str, float, float]] = []
    for w in wallets:
        sol_bal = rpc_get_sol_balance(w)
        usdc_bal = rpc_get_spl_mint_balance(w, USDC_MINT)
        print(f"Balance for {w[:6]}...: SOL={sol_bal:.6f} USDC={usdc_bal:.6f}")
        per_rows.append((w, sol_bal, usdc_bal))

    total_sol = 0.0
    total_usdc = 0.0

    # Per wallet rows
    for (w, sol_bal, usdc_bal) in per_rows:
        total_sol += sol_bal
        total_usdc += usdc_bal

        prev = get_prev_wallet_entry(perwallet_db, per_wallet_addr_prop, w, per_date_prop)
        prev_sol = read_number_prop(prev, per_end_prop)
        prev_usdc = read_number_prop(prev, per_usdc_end_prop)

        sol_delta = sol_bal - prev_sol
        usdc_delta = usdc_bal - prev_usdc

        props = {
            per_title_prop: prop_title(w),
            per_date_prop: prop_date(today),
            per_end_prop: prop_number(sol_bal),
            per_delta_prop: prop_number(sol_delta),
            per_wallet_addr_prop: prop_rich_text(w),
            per_usdc_end_prop: prop_number(usdc_bal),
            per_usdc_delta_prop: prop_number(usdc_delta),
        }
        notion_create_page(perwallet_db, props)

    # Total row
    prev_total = get_prev_total_entry(dailytotal_db, total_date_prop)
    prev_total_sol = read_number_prop(prev_total, total_end_prop)
    prev_total_usdc = read_number_prop(prev_total, total_usdc_end_prop)

    total_delta = total_sol - prev_total_sol
    total_usdc_delta = total_usdc - prev_total_usdc

    total_props_payload = {
        total_title_prop: prop_title(f"{total_sol:.2f} SOL"),
        total_date_prop: prop_date(today),
        total_end_prop: prop_number(total_sol),
        total_delta_prop: prop_number(total_delta),
        total_usdc_end_prop: prop_number(total_usdc),
        total_usdc_delta_prop: prop_number(total_usdc_delta),
    }
    notion_create_page(dailytotal_db, total_props_payload)

    print(f"OK: wrote {len(wallets)} per-wallet rows + 1 total row for {today}")


if __name__ == "__main__":
    main()
