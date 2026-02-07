#!/usr/bin/env python3
import os
import json
import time
import datetime
import urllib.request
import urllib.error

# ----------------------------
# ENV (required)
# ----------------------------
NOTION_TOKEN = os.environ["NOTION_TOKEN"]
NOTION_DB_PERWALLET = os.environ["NOTION_DB_PERWALLET"]
NOTION_DB_DAILYTOTAL = os.environ["NOTION_DB_DAILYTOTAL"]
WALLETS_RAW = os.environ["WALLETS_CSV"]

SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
NOTION_VERSION = "2022-06-28"

# Optional overrides (if you want)
TITLE_PROP_PERWALLET = os.getenv("TITLE_PROP_PERWALLET", "").strip() or None
TOTAL_TITLE_PROP = os.getenv("TOTAL_TITLE_PROP", "").strip() or None

# USDC mint (Solana mainnet)
USDC_MINT = os.getenv("USDC_MINT", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")

# Property names (your Notion columns)
# If any of these do NOT exist in your DB, this script will now skip them safely.
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

# ----------------------------
# HELPERS
# ----------------------------
def now_date_iso():
    return datetime.date.today().isoformat()

def notion_headers():
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }

def http_json(url, method="POST", headers=None, body_obj=None, timeout=30):
    if headers is None:
        headers = {}
    data = None
    if body_obj is not None:
        data = json.dumps(body_obj).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, method=method, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        # CRITICAL: show Notion/Solana error body so we stop guessing
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = "<no body>"
        raise Exception(f"HTTP {e.code} calling {url}\nResponse body:\n{body}") from None

def retry(fn, tries=6, base_sleep=1.5):
    last_err = None
    for i in range(tries):
        try:
            return fn()
        except Exception as e:
            last_err = e
            # Retry only on likely transient cases
            msg = str(e)
            if "HTTP 429" in msg or "HTTP 500" in msg or "HTTP 502" in msg or "HTTP 503" in msg or "HTTP 504" in msg:
                time.sleep(base_sleep * (2 ** i))
                continue
            # Not transient -> fail immediately
            raise
    raise Exception(f"Failed after retries. Last error: {last_err}")

def parse_wallets(raw: str):
    # Accept commas, newlines, spaces. Dedupe. Ignore junk.
    parts = []
    for line in raw.replace(",", "\n").splitlines():
        w = line.strip()
        if not w:
            continue
        if len(w) < 32:
            continue
        parts.append(w)

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
    return retry(lambda: http_json(SOLANA_RPC_URL, headers={}, body_obj=payload, timeout=30))

def rpc_get_sol_balance(pubkey: str) -> float:
    j = rpc_call("getBalance", [pubkey, {"commitment":"confirmed"}])
    if "error" in j:
        raise Exception(f"Solana RPC error: {j['error']}")
    lamports = j["result"]["value"]
    return lamports / 1_000_000_000

def rpc_get_usdc_balance(pubkey: str) -> float:
    # Correct method: token accounts filtered by mint, jsonParsed, sum them
    j = rpc_call(
        "getTokenAccountsByOwner",
        [pubkey, {"mint": USDC_MINT}, {"encoding":"jsonParsed", "commitment":"confirmed"}]
    )
    if "error" in j:
        raise Exception(f"Solana RPC error: {j['error']}")

    res = j["result"]
    total_base = 0
    decimals = None
    for it in res.get("value", []):
        info = it["account"]["data"]["parsed"]["info"]
        ta = info["tokenAmount"]
        total_base += int(ta["amount"])
        if decimals is None:
            decimals = int(ta["decimals"])

    if decimals is None:
        return 0.0
    return total_base / (10 ** decimals)

# ----------------------------
# NOTION API
# ----------------------------
def notion_get_database(db_id):
    url = f"https://api.notion.com/v1/databases/{db_id}"
    return retry(lambda: http_json(url, method="GET", headers=notion_headers(), body_obj=None, timeout=30))

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
    body = {"parent": {"database_id": parent_db_id}, "properties": props}
    return retry(lambda: http_json(url, headers=notion_headers(), body_obj=body, timeout=30))

def notion_title_prop(value: str):
    return {"title":[{"text":{"content": value}}]}

def notion_number_prop(value):
    return {"number": None if value is None else float(value)}

def notion_date_prop(date_iso: str):
    return {"date": {"start": date_iso}}

def detect_title_prop_name(db_schema, override_name: str | None):
    """
    Notion DB always has exactly one 'title' type property.
    If override is supplied but wrong, we fall back to detected title prop.
    """
    props = db_schema.get("properties", {})
    detected = None
    for name, meta in props.items():
        if meta.get("type") == "title":
            detected = name
            break

    if override_name and override_name in props and props[override_name].get("type") == "title":
        return override_name
    return detected

def filter_props_to_existing(db_schema, props_to_send: dict):
    """
    Only send properties that exist in the DB schema.
    Prevents Notion 400 for missing property names.
    """
    existing = set(db_schema.get("properties", {}).keys())
    return {k: v for k, v in props_to_send.items() if k in existing}

def read_number_prop(page, prop_name):
    try:
        p = page["properties"][prop_name]
        if p["type"] == "number":
            return p["number"]
    except Exception:
        return None
    return None

def get_last_entry(db_id, date_prop_name):
    res = notion_query_database(
        db_id,
        sorts=[{"property": date_prop_name, "direction":"descending"}],
        page_size=1
    )
    results = res.get("results", [])
    return results[0] if results else None

def get_last_wallet_entry(wallet: str):
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

# ----------------------------
# MAIN
# ----------------------------
def main():
    wallets = parse_wallets(WALLETS_RAW)
    if not wallets:
        raise Exception("No wallets parsed from WALLETS_CSV. Put wallets comma-separated OR one per line.")

    today = now_date_iso()

    # Load Notion DB schemas (we need these to auto-detect title prop + avoid 400)
    schema_total = notion_get_database(NOTION_DB_DAILYTOTAL)
    schema_wallet = notion_get_database(NOTION_DB_PERWALLET)

    total_title_prop = detect_title_prop_name(schema_total, TOTAL_TITLE_PROP)
    wallet_title_prop = detect_title_prop_name(schema_wallet, TITLE_PROP_PERWALLET)

    if not total_title_prop:
        raise Exception("Could not detect the Title property in Daily Total DB.")
    if not wallet_title_prop:
        raise Exception("Could not detect the Title property in Per Wallet DB.")

    # Pull balances
    per_wallet = []
    total_sol = 0.0
    total_usdc = 0.0

    for w in wallets:
        sol = rpc_get_sol_balance(w)
        usdc = rpc_get_usdc_balance(w)
        per_wallet.append((w, sol, usdc))
        total_sol += sol
        total_usdc += usdc

    # ---- DAILY TOTAL ----
    last_total = get_last_entry(NOTION_DB_DAILYTOTAL, TOTAL_DATE_PROP)
    last_total_sol = read_number_prop(last_total, TOTAL_SOL_END_PROP) if last_total else None
    last_total_usdc = read_number_prop(last_total, TOTAL_USDC_END_PROP) if last_total else None

    total_sol_delta = None if last_total_sol is None else (total_sol - float(last_total_sol))
    total_usdc_delta = None if last_total_usdc is None else (total_usdc - float(last_total_usdc))

    total_props_raw = {
        total_title_prop: notion_title_prop(f"{round(total_sol, 2)} SOL"),
        TOTAL_DATE_PROP: notion_date_prop(today),
        TOTAL_SOL_END_PROP: notion_number_prop(total_sol),
        TOTAL_SOL_START_PROP: notion_number_prop(last_total_sol),
        TOTAL_SOL_DELTA_PROP: notion_number_prop(total_sol_delta),
        TOTAL_USDC_END_PROP: notion_number_prop(total_usdc),
        TOTAL_USDC_START_PROP: notion_number_prop(last_total_usdc),
        TOTAL_USDC_DELTA_PROP: notion_number_prop(total_usdc_delta),
    }
    total_props = filter_props_to_existing(schema_total, total_props_raw)
    notion_create_page(NOTION_DB_DAILYTOTAL, total_props)

    # ---- PER WALLET ----
    for (w, sol, usdc) in per_wallet:
        last_w = get_last_wallet_entry(w)
        last_sol = read_number_prop(last_w, PERWALLET_SOL_END_PROP) if last_w else None
        last_usdc = read_number_prop(last_w, PERWALLET_USDC_END_PROP) if last_w else None

        sol_delta = None if last_sol is None else (sol - float(last_sol))
        usdc_delta = None if last_usdc is None else (usdc - float(last_usdc))

        wallet_props_raw = {
            wallet_title_prop: notion_title_prop(w),
            PERWALLET_DATE_PROP: notion_date_prop(today),
            PERWALLET_WALLET_ADDR_PROP: {"rich_text":[{"text":{"content": w}}]},
            PERWALLET_SOL_END_PROP: notion_number_prop(sol),
            PERWALLET_SOL_START_PROP: notion_number_prop(last_sol),
            PERWALLET_SOL_DELTA_PROP: notion_number_prop(sol_delta),
            PERWALLET_USDC_END_PROP: notion_number_prop(usdc),
            PERWALLET_USDC_START_PROP: notion_number_prop(last_usdc),
            PERWALLET_USDC_DELTA_PROP: notion_number_prop(usdc_delta),
        }
        wallet_props = filter_props_to_existing(schema_wallet, wallet_props_raw)
        notion_create_page(NOTION_DB_PERWALLET, wallet_props)

    print(f"OK wallets={len(wallets)} total_sol={total_sol:.6f} total_usdc={total_usdc:.6f}")
    print(f"Notion title props: daily_total='{total_title_prop}', per_wallet='{wallet_title_prop}'")

if __name__ == "__main__":
    main()
