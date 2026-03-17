# sol_multi_to_notion.py
# Writes daily SOL + USDC balances to Notion (Per Wallet + Daily Total),
# computes deltas, and writes SOL Baseline once per day.

import os, sys, json, time, random, urllib.request, urllib.error
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional, Tuple

# ----------------------------
# ENV / CONFIG
# ----------------------------
NOTION_TOKEN = os.getenv("NOTION_TOKEN", "").strip()
DB_PER = os.getenv("NOTION_DB_PERWALLET", "").strip()
DB_TOTAL = os.getenv("NOTION_DB_DAILYTOTAL", "").strip()
WALLETS_RAW = os.getenv("WALLETS_CSV", "")

RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com").strip()

# Notion property names
TITLE_PROP_PERWALLET = os.getenv("TITLE_PROP_PERWALLET", "Wallet").strip()
TOTAL_TITLE_PROP = os.getenv("TOTAL_TITLE_PROP", "Name").strip()
DATE_PROP = os.getenv("DATE_PROP", "Date").strip()

# Numeric property names
PROP_SOL_END = os.getenv("PROP_SOL_END", "End Balance").strip()
PROP_SOL_DELTA = os.getenv("PROP_SOL_DELTA", "Delta").strip()
PROP_USDC_END = os.getenv("PROP_USDC_END", "USDC End Balance").strip()
PROP_USDC_DELTA = os.getenv("PROP_USDC_DELTA", "USDC Delta").strip()
PROP_SOL_BASELINE = os.getenv("PROP_SOL_BASELINE", "SOL Baseline").strip()

# USDC mint (mainnet)
USDC_MINT = os.getenv(
    "USDC_MINT",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
).strip()

# Local date label
LOCAL_TZ = ZoneInfo(os.getenv("LOCAL_TZ", "Australia/Sydney"))

NOTION_VERSION = os.getenv("NOTION_VERSION", "2022-06-28").strip()

RPC_TIMEOUT = int(os.getenv("RPC_TIMEOUT", "30"))
RPC_RETRIES = int(os.getenv("RPC_RETRIES", "10"))
NOTION_TIMEOUT = int(os.getenv("NOTION_TIMEOUT", "30"))
NOTION_RETRIES = int(os.getenv("NOTION_RETRIES", "6"))

# Added to control pacing
RPC_CALL_DELAY = float(os.getenv("RPC_CALL_DELAY", "0.6"))
RPC_WALLET_DELAY = float(os.getenv("RPC_WALLET_DELAY", "1.0"))
RPC_BACKOFF_CAP = float(os.getenv("RPC_BACKOFF_CAP", "45"))

# ----------------------------
# UTILS
# ----------------------------
def fail(msg: str):
    print(f"ERROR: {msg}")
    sys.exit(1)

def r2(x: Optional[float]) -> Optional[float]:
    return None if x is None else round(float(x), 2)

def backoff_sleep(attempt: int, base: float = 2.0, cap: float = RPC_BACKOFF_CAP):
    delay = min((base ** attempt) + random.uniform(0.3, 1.2), cap)
    print(f"Retrying after {delay:.2f}s...")
    time.sleep(delay)

def http_json(url: str, method: str, headers: dict, body: Optional[dict], timeout: int, retries: int) -> dict:
    data = None if body is None else json.dumps(body).encode("utf-8")
    last_err = None

    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, method=method.upper())
            for k, v in headers.items():
                req.add_header(k, v)

            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                return json.loads(raw) if raw else {}

        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass

            last_err = f"HTTP {e.code} {e.reason}: {detail}"

            if e.code == 429:
                print(f"Rate limit hit: {last_err}")
                backoff_sleep(attempt + 1, base=2.4)
                continue

            if e.code in (408, 425, 500, 502, 503, 504):
                print(f"Transient HTTP error: {last_err}")
                backoff_sleep(attempt + 1)
                continue

            raise Exception(last_err)

        except Exception as e:
            last_err = str(e)
            print(f"Request error: {last_err}")
            backoff_sleep(attempt + 1)

    if "429" in str(last_err) or "max usage reached" in str(last_err):
        raise Exception(
            "RPC quota/rate limit exceeded. "
            f"Last error: {last_err}. "
            "Use a paid RPC, increase delays, or reduce wallet count per run."
        )

    raise Exception(f"HTTP failed after retries. Last error: {last_err}")

def notion_headers() -> dict:
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Content-Type": "application/json",
        "Notion-Version": NOTION_VERSION,
    }

def parse_wallets(raw: str) -> list[str]:
    parts = [p.strip() for p in raw.replace("\n", ",").split(",")]
    out, seen = [], set()
    for p in parts:
        if not p:
            continue
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out

def today_local_iso() -> str:
    return datetime.now(LOCAL_TZ).date().isoformat()

# ----------------------------
# SOLANA RPC
# ----------------------------
def rpc_post(payload: dict) -> dict:
    return http_json(
        url=RPC_URL,
        method="POST",
        headers={"Content-Type": "application/json"},
        body=payload,
        timeout=RPC_TIMEOUT,
        retries=RPC_RETRIES,
    )

def rpc_get_sol_balance(wallet: str) -> float:
    payload = {"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [wallet]}
    res = rpc_post(payload)
    if "error" in res:
        raise Exception(f"RPC error getBalance({wallet}): {res['error']}")
    if "result" not in res or "value" not in res["result"]:
        raise Exception(f"RPC bad response getBalance({wallet}): {res}")
    return res["result"]["value"] / 1e9

def rpc_get_usdc_balance(wallet: str) -> float:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTokenAccountsByOwner",
        "params": [
            wallet,
            {"mint": USDC_MINT},
            {"encoding": "jsonParsed"},
        ],
    }
    res = rpc_post(payload)
    if "error" in res:
        raise Exception(f"RPC error getTokenAccountsByOwner({wallet}): {res['error']}")
    val = res.get("result", {}).get("value")
    if val is None:
        raise Exception(f"RPC bad response getTokenAccountsByOwner({wallet}): {res}")

    total = 0.0
    for acc in val:
        try:
            info = acc["account"]["data"]["parsed"]["info"]
            ta = info["tokenAmount"]
            ui_amt = ta.get("uiAmount")
            if ui_amt is not None:
                total += float(ui_amt)
            else:
                amt = int(ta.get("amount", "0"))
                dec = int(ta.get("decimals", 0))
                total += (amt / (10 ** dec)) if dec else float(amt)
        except Exception:
            continue
    return total

# ----------------------------
# NOTION
# ----------------------------
def notion_get_database(db_id: str) -> dict:
    return http_json(
        url=f"https://api.notion.com/v1/databases/{db_id}",
        method="GET",
        headers=notion_headers(),
        body=None,
        timeout=NOTION_TIMEOUT,
        retries=NOTION_RETRIES,
    )

def notion_query(db_id: str, body: dict) -> dict:
    return http_json(
        url=f"https://api.notion.com/v1/databases/{db_id}/query",
        method="POST",
        headers=notion_headers(),
        body=body,
        timeout=NOTION_TIMEOUT,
        retries=NOTION_RETRIES,
    )

def notion_create_page(db_id: str, props: dict) -> dict:
    return http_json(
        url="https://api.notion.com/v1/pages",
        method="POST",
        headers=notion_headers(),
        body={"parent": {"database_id": db_id}, "properties": props},
        timeout=NOTION_TIMEOUT,
        retries=NOTION_RETRIES,
    )

def notion_update_page(page_id: str, props: dict) -> dict:
    return http_json(
        url=f"https://api.notion.com/v1/pages/{page_id}",
        method="PATCH",
        headers=notion_headers(),
        body={"properties": props},
        timeout=NOTION_TIMEOUT,
        retries=NOTION_RETRIES,
    )

def get_number(page: dict, prop: str) -> Optional[float]:
    try:
        p = page["properties"][prop]
        if p.get("type") == "number":
            return p.get("number")
    except Exception:
        return None
    return None

def get_page_id(page: dict) -> str:
    return page.get("id")

def find_total_page_for_date(db_id: str, date_iso: str) -> Optional[dict]:
    body = {
        "filter": {"property": DATE_PROP, "date": {"equals": date_iso}},
        "page_size": 1
    }
    res = notion_query(db_id, body)
    results = res.get("results", [])
    return results[0] if results else None

def find_latest_total_before_date(db_id: str, date_iso: str) -> Optional[dict]:
    body = {
        "filter": {"property": DATE_PROP, "date": {"before": date_iso}},
        "sorts": [{"property": DATE_PROP, "direction": "descending"}],
        "page_size": 1
    }
    res = notion_query(db_id, body)
    results = res.get("results", [])
    return results[0] if results else None

def find_per_wallet_page_for_date(db_id: str, wallet: str, date_iso: str) -> Optional[dict]:
    body = {
        "filter": {
            "and": [
                {"property": TITLE_PROP_PERWALLET, "title": {"equals": wallet}},
                {"property": DATE_PROP, "date": {"equals": date_iso}},
            ]
        },
        "page_size": 1
    }
    res = notion_query(db_id, body)
    results = res.get("results", [])
    return results[0] if results else None

def find_latest_per_wallet_before_date(db_id: str, wallet: str, date_iso: str) -> Optional[dict]:
    body = {
        "filter": {
            "and": [
                {"property": TITLE_PROP_PERWALLET, "title": {"equals": wallet}},
                {"property": DATE_PROP, "date": {"before": date_iso}},
            ]
        },
        "sorts": [{"property": DATE_PROP, "direction": "descending"}],
        "page_size": 1
    }
    res = notion_query(db_id, body)
    results = res.get("results", [])
    return results[0] if results else None

def validate_db_schema(db_id: str, required: list[Tuple[str, str]]):
    db = notion_get_database(db_id)
    props = db.get("properties", {})
    missing = []
    wrong_type = []

    for name, want_type in required:
        if name not in props:
            missing.append(name)
        else:
            got = props[name].get("type")
            if got != want_type:
                wrong_type.append((name, got, want_type))

    if missing or wrong_type:
        available = ", ".join(sorted(props.keys()))
        msg = []
        if missing:
            msg.append(f"Missing properties in DB {db_id}: {missing}")
        if wrong_type:
            msg.append(f"Wrong property types in DB {db_id}: {wrong_type}")
        msg.append(f"Available properties: {available}")
        fail("\n".join(msg))

# ----------------------------
# MAIN
# ----------------------------
def main():
    if not NOTION_TOKEN:
        fail("NOTION_TOKEN missing")
    if not DB_PER:
        fail("NOTION_DB_PERWALLET missing")
    if not DB_TOTAL:
        fail("NOTION_DB_DAILYTOTAL missing")

    wallets = parse_wallets(WALLETS_RAW)
    if not wallets:
        fail("WALLETS_CSV is empty or invalid")

    if not RPC_URL.startswith("https://"):
        fail(f"SOLANA_RPC_URL must be https:// (got {RPC_URL})")

    validate_db_schema(DB_TOTAL, [
        (TOTAL_TITLE_PROP, "title"),
        (DATE_PROP, "date"),
        (PROP_SOL_END, "number"),
        (PROP_SOL_DELTA, "number"),
        (PROP_USDC_END, "number"),
        (PROP_USDC_DELTA, "number"),
        (PROP_SOL_BASELINE, "number"),
    ])
    validate_db_schema(DB_PER, [
        (TITLE_PROP_PERWALLET, "title"),
        (DATE_PROP, "date"),
        (PROP_SOL_END, "number"),
        (PROP_SOL_DELTA, "number"),
        (PROP_USDC_END, "number"),
        (PROP_USDC_DELTA, "number"),
    ])

    date_iso = today_local_iso()

    # Fetch balances with gentler pacing
    per_rows = []
    for idx, w in enumerate(wallets, start=1):
        print(f"Fetching wallet {idx}/{len(wallets)}: {w}")

        sol = r2(rpc_get_sol_balance(w))
        time.sleep(RPC_CALL_DELAY)

        usdc = r2(rpc_get_usdc_balance(w))
        time.sleep(RPC_WALLET_DELAY)

        per_rows.append((w, sol, usdc))

    total_sol = r2(sum(sol for _, sol, _ in per_rows))
    total_usdc = r2(sum(usdc for _, _, usdc in per_rows))

    prev_total_page = find_latest_total_before_date(DB_TOTAL, date_iso)
    prev_total_sol = get_number(prev_total_page, PROP_SOL_END) if prev_total_page else None
    prev_total_usdc = get_number(prev_total_page, PROP_USDC_END) if prev_total_page else None

    total_sol_delta = None if prev_total_sol is None else r2(total_sol - float(prev_total_sol))
    total_usdc_delta = None if prev_total_usdc is None else r2(total_usdc - float(prev_total_usdc))

    today_total_page = find_total_page_for_date(DB_TOTAL, date_iso)

    baseline_to_set = total_sol
    existing_baseline = get_number(today_total_page, PROP_SOL_BASELINE) if today_total_page else None
    should_set_baseline = (existing_baseline is None)

    total_props_update = {
        TOTAL_TITLE_PROP: {"title": [{"text": {"content": f"{total_sol:.2f} SOL"}}]},
        DATE_PROP: {"date": {"start": date_iso}},
        PROP_SOL_END: {"number": total_sol},
        PROP_SOL_DELTA: {"number": total_sol_delta},
        PROP_USDC_END: {"number": total_usdc},
        PROP_USDC_DELTA: {"number": total_usdc_delta},
    }

    if should_set_baseline:
        total_props_update[PROP_SOL_BASELINE] = {"number": baseline_to_set}

    if today_total_page:
        notion_update_page(get_page_id(today_total_page), total_props_update)
    else:
        if PROP_SOL_BASELINE not in total_props_update:
            total_props_update[PROP_SOL_BASELINE] = {"number": baseline_to_set}
        notion_create_page(DB_TOTAL, total_props_update)

    for w, sol, usdc in per_rows:
        prev_wallet_page = find_latest_per_wallet_before_date(DB_PER, w, date_iso)
        prev_w_sol = get_number(prev_wallet_page, PROP_SOL_END) if prev_wallet_page else None
        prev_w_usdc = get_number(prev_wallet_page, PROP_USDC_END) if prev_wallet_page else None

        w_sol_delta = None if prev_w_sol is None else r2(sol - float(prev_w_sol))
        w_usdc_delta = None if prev_w_usdc is None else r2(usdc - float(prev_w_usdc))

        wallet_props = {
            TITLE_PROP_PERWALLET: {"title": [{"text": {"content": w}}]},
            DATE_PROP: {"date": {"start": date_iso}},
            PROP_SOL_END: {"number": sol},
            PROP_SOL_DELTA: {"number": w_sol_delta},
            PROP_USDC_END: {"number": usdc},
            PROP_USDC_DELTA: {"number": w_usdc_delta},
        }

        today_wallet_page = find_per_wallet_page_for_date(DB_PER, w, date_iso)
        if today_wallet_page:
            notion_update_page(get_page_id(today_wallet_page), wallet_props)
        else:
            notion_create_page(DB_PER, wallet_props)

    print(f"{date_iso} | wallets={len(per_rows)}")
    print(f"TOTAL SOL={total_sol:.2f}  Δ={('None' if total_sol_delta is None else f'{total_sol_delta:+.2f}')}")
    print(f"TOTAL USDC={total_usdc:.2f}  Δ={('None' if total_usdc_delta is None else f'{total_usdc_delta:+.2f}')}")
    if should_set_baseline:
        print(f"SOL Baseline set: {baseline_to_set:.2f}")
    else:
        print(f"SOL Baseline already set: {existing_baseline}")

if __name__ == "__main__":
    main()
