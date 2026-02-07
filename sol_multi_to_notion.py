#!/usr/bin/env python3
import os
import json
import time
import datetime
import urllib.request
import urllib.error

# ----------------------------
# CONFIG (property names)
# ----------------------------
PERWALLET_DATE_PROP = os.getenv("PERWALLET_DATE_PROP", "Date")
PERWALLET_SOL_END_PROP = os.getenv("PERWALLET_SOL_END_PROP", "End Balance")
PERWALLET_SOL_START_PROP = os.getenv("PERWALLET_SOL_START_PROP", "Start Balance")
PERWALLET_SOL_DELTA_PROP = os.getenv("PERWALLET_SOL_DELTA_PROP", "Delta")
PERWALLET_WALLET_ADDR_PROP = os.getenv("PERWALLET_WALLET_ADDR_PROP", "Wallet Address")
PERWALLET_USDC_END_PROP = os.getenv("PERWALLET_USDC_END_PROP", "USDC End Balance")
PERWALLET_USDC_START_PROP = os.getenv("PERWALLET_USDC_START_PROP", "USDC Start Balance")
PERWALLET_USDC_DELTA_PROP = os.getenv("PERWALLET_USDC_DELTA_PROP", "USDC Delta")

TOTAL_DATE_PROP = os.getenv("TOTAL_DATE_PROP", "Date")
TOTAL_SOL_END_PROP = os.getenv("TOTAL_SOL_END_PROP", "End Balance")
TOTAL_SOL_START_PROP = os.getenv("TOTAL_SOL_START_PROP", "Start Balance")
TOTAL_SOL_DELTA_PROP = os.getenv("TOTAL_SOL_DELTA_PROP", "Delta")
TOTAL_USDC_END_PROP = os.getenv("TOTAL_USDC_END_PROP", "USDC End Balance")
TOTAL_USDC_START_PROP = os.getenv("TOTAL_USDC_START_PROP", "USDC Start Balance")
TOTAL_USDC_DELTA_PROP = os.getenv("TOTAL_USDC_DELTA_PROP", "USDC Delta")

# Title prop overrides (critical with Notion)
TITLE_PROP_PERWALLET = os.getenv("TITLE_PROP_PERWALLET", "Name")
TOTAL_TITLE_PROP = os.getenv("TOTAL_TITLE_PROP", "Name")

# USDC mint (Solana mainnet)
USDC_MINT = os.getenv("USDC_MINT", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")

# ----------------------------
# ENV
# ----------------------------
NOTION_TOKEN = os.environ["NOTION_TOKEN"]
NOTION_DB_PERWALLET = os.environ["NOTION_DB_PERWALLET"]
NOTION_DB_DAILYTOTAL = os.environ["NOTION_DB_DAILYTOTAL"]
WALLETS_RAW = os.environ["WALLETS_CSV"]
SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")

NOTION_VERSION = "2022-06-28"

# ----------------------------
# HELPERS
# ----------------------------
def now_date_iso():
    # Use local date (AEST on runner doesn’t matter if you want “today”)
    return datetime.date.today().isoformat()

def http_json(url, method="POST", headers=None, body_obj=None, timeout=30):
    if headers is None:
        headers = {}
    data = None
    if body_obj is not None:
        data = json.dumps(body_obj).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw)

def retry(fn, tries=6, base_sleep=1.5):
    last_err = None
    for i in range(tries):
        try:
            return fn()
        except urllib.error.HTTPError as e:
            last_err = e
            # handle 429 + transient
            if e.code in (429, 500, 502, 503, 504):
                time.sleep(base_sleep * (2 ** i))
                continue
            raise
        except Exception as e:
            last_err = e
            time.sleep(base_sleep * (2 ** i))
    raise Exception(f"Failed after retries. Last error: {last_err}")

def parse_wallets(raw: str):
    # Accept:
    # - one-per-line
    # - comma separated
    # - with accidental spaces
    # - ignores empty
    parts = []
    for line in raw.replace(",", "\n").splitlines():
        w = line.strip()
        if not w:
            continue
        # basic sanity: sol addresses are usually 32-44 chars base58, but we just enforce non-trivial
        if len(w) < 32:
            continue
        parts.append(w)
    # de-dupe while keeping order
    seen = set()
    out = []
    for w in parts:
        if w in seen:
            continue
        seen.add(w)
        out.append(w)
    return out

# ----------------------------
# SOLANA RPC
# ----------------------------
_rpc_id = 1
def rpc_call(method, params):
    global _rpc_id
    payload = {"jsonrpc":"2.0","id":_rpc_id,"method":method,"params":params}
    _rpc_id += 1

    def _do():
        return http_json(SOLANA_RPC_URL, headers={}, body_obj=payload, timeout=30)

    j = retry(_do)
    if "error" in j:
        raise Exception(f"Solana RPC error: {j['error']}")
    return j["result"]

def rpc_get_sol_balance(pubkey: str) -> float:
    res = rpc_call("getBalance", [pubkey, {"commitment":"confirmed"}])
    lamports = res["value"]
    return lamports / 1_000_000_000

def rpc_get_usdc_balance(pubkey: str) -> float:
    """
    Correct USDC:
    - Use getTokenAccountsByOwner filtered by mint
    - Sum tokenAmount.amount using tokenAmount.decimals
    This works even if wallet has multiple token accounts.
    """
    res = rpc_call(
        "getTokenAccountsByOwner",
        [pubkey, {"mint": USDC_MINT}, {"encoding":"jsonParsed", "commitment":"confirmed"}]
    )
    total_base = 0
    decimals = None
    for it in res.get("value", []):
        info = it["account"]["data"]["parsed"]["info"]
        ta = info["tokenAmount"]
        amt = int(ta["amount"])
        total_base += amt
        if decimals is None:
            decimals = int(ta["decimals"])
    if decimals is None:
        return 0.0
    return total_base / (10 ** decimals)

# ----------------------------
# NOTION API
# ----------------------------
def notion_headers():
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }

def notion_query_database(db_id, filter_obj=None, sorts=None, page_size=100):
    url = f"https://api.notion.com/v1/databases/{db_id}/query"
    body = {"page_size": page_size}
    if filter_obj:
        body["filter"] = filter_obj
    if sorts:
        body["sorts"] = sorts
    return retry(lambda: http_json(url, headers=notion_headers(), body_obj=body, timeout=30))

def notion_create_page(parent_db_id, props):
    url = "https://api.notion.com/v1/pages"
    body = {
        "parent": {"database_id": parent_db_id},
        "properties": props
    }
    return retry(lambda: http_json(url, headers=notion_headers(), body_obj=body, timeout=30))

def notion_update_page(page_id, props):
    url = f"https://api.notion.com/v1/pages/{page_id}"
    body = {"properties": props}
    return retry(lambda: http_json(url, headers=notion_headers(), body_obj=body, timeout=30))

def notion_title_prop(value: str):
    return {"title":[{"text":{"content": value}}]}

def notion_number_prop(value):
    return {"number": None if value is None else float(value)}

def notion_date_prop(date_iso: str):
    return {"date": {"start": date_iso}}

def get_last_entry(db_id, date_prop_name):
    # last by Date desc
    res = notion_query_database(
        db_id,
        filter_obj=None,
        sorts=[{"property": date_prop_name, "direction":"descending"}],
        page_size=1
    )
    results = res.get("results", [])
    return results[0] if results else None

def get_last_wallet_entry(wallet: str):
    # filter by Wallet Address equals wallet, sort Date desc
    res = notion_query_database(
        NOTION_DB_PERWALLET,
        filter_obj={
            "property": PERWALLET_WALLET_ADDR_PROP,
            "rich_text": {"equals": wallet}
        },
        sorts=[{"property": PERWALLET_DATE_PROP, "direction":"descending"}],
        page_size=1
    )
    results = res.get("results", [])
    return results[0] if results else None

def read_number_prop(page, prop_name):
    try:
        p = page["properties"][prop_name]
        if p["type"] == "number":
            return p["number"]
    except Exception:
        return None
    return None

# ----------------------------
# MAIN
# ----------------------------
def main():
    wallets = parse_wallets(WALLETS_RAW)
    if not wallets:
        raise Exception("No wallets parsed from WALLETS_CSV. Use one per line or comma-separated.")

    today = now_date_iso()

    # Pull current balances
    per_wallet = []
    total_sol = 0.0
    total_usdc = 0.0

    for w in wallets:
        sol = rpc_get_sol_balance(w)
        usdc = rpc_get_usdc_balance(w)
        per_wallet.append((w, sol, usdc))
        total_sol += sol
        total_usdc += usdc

    # ---- DAILY TOTAL DB ----
    last_total = get_last_entry(NOTION_DB_DAILYTOTAL, TOTAL_DATE_PROP)
    last_total_sol = read_number_prop(last_total, TOTAL_SOL_END_PROP) if last_total else None
    last_total_usdc = read_number_prop(last_total, TOTAL_USDC_END_PROP) if last_total else None

    total_sol_delta = None if last_total_sol is None else (total_sol - float(last_total_sol))
    total_usdc_delta = None if last_total_usdc is None else (total_usdc - float(last_total_usdc))

    total_props = {
        TOTAL_TITLE_PROP: notion_title_prop(f"{round(total_sol, 2)} SOL"),
        TOTAL_DATE_PROP: notion_date_prop(today),
        TOTAL_SOL_END_PROP: notion_number_prop(total_sol),
        TOTAL_SOL_START_PROP: notion_number_prop(last_total_sol),
        TOTAL_SOL_DELTA_PROP: notion_number_prop(total_sol_delta),
        TOTAL_USDC_END_PROP: notion_number_prop(total_usdc),
        TOTAL_USDC_START_PROP: notion_number_prop(last_total_usdc),
        TOTAL_USDC_DELTA_PROP: notion_number_prop(total_usdc_delta),
    }

    notion_create_page(NOTION_DB_DAILYTOTAL, total_props)

    # ---- PER WALLET DB ----
    for (w, sol, usdc) in per_wallet:
        last_w = get_last_wallet_entry(w)
        last_sol = read_number_prop(last_w, PERWALLET_SOL_END_PROP) if last_w else None
        last_usdc = read_number_prop(last_w, PERWALLET_USDC_END_PROP) if last_w else None

        sol_delta = None if last_sol is None else (sol - float(last_sol))
        usdc_delta = None if last_usdc is None else (usdc - float(last_usdc))

        props = {
            TITLE_PROP_PERWALLET: notion_title_prop(w),
            PERWALLET_DATE_PROP: notion_date_prop(today),
            PERWALLET_WALLET_ADDR_PROP: {"rich_text":[{"text":{"content": w}}]},
            PERWALLET_SOL_END_PROP: notion_number_prop(sol),
            PERWALLET_SOL_START_PROP: notion_number_prop(last_sol),
            PERWALLET_SOL_DELTA_PROP: notion_number_prop(sol_delta),
            PERWALLET_USDC_END_PROP: notion_number_prop(usdc),
            PERWALLET_USDC_START_PROP: notion_number_prop(last_usdc),
            PERWALLET_USDC_DELTA_PROP: notion_number_prop(usdc_delta),
        }

        notion_create_page(NOTION_DB_PERWALLET, props)

    print(f"OK: wallets={len(wallets)} total_sol={total_sol:.6f} total_usdc={total_usdc:.6f}")

if __name__ == "__main__":
    main()
