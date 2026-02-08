#!/usr/bin/env python3
import json
import os
import sys
import time
import random
import urllib.request
import urllib.error
from datetime import datetime, timezone

# -----------------------------
# Env
# -----------------------------
NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "").strip()
NOTION_DB_DAILYTOTAL = os.environ.get("NOTION_DB_DAILYTOTAL", "").strip()

DATE_PROP = os.environ.get("DATE_PROP", "Date").strip()
TITLE_PROP_DAILY = os.environ.get("TOTAL_TITLE_PROP", "Name").strip()

# Must match your Notion column name EXACTLY
BASELINE_PROP = os.environ.get("BASELINE_PROP", "SOL Baseline").strip()

WALLETS_CSV = os.environ.get("WALLETS_CSV", "").strip()
SOLANA_RPC_URL = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com").strip()

NOTION_VERSION = os.environ.get("NOTION_VERSION", "2022-06-28").strip()

RPC_TIMEOUT_SECS = int(os.environ.get("RPC_TIMEOUT_SECS", "30"))
RPC_RETRIES = int(os.environ.get("RPC_RETRIES", "6"))

NOTION_TIMEOUT_SECS = int(os.environ.get("NOTION_TIMEOUT_SECS", "30"))
NOTION_RETRIES = int(os.environ.get("NOTION_RETRIES", "5"))

# -----------------------------
# Helpers
# -----------------------------
def die(msg: str, code: int = 1) -> None:
    print(msg, file=sys.stderr)
    sys.exit(code)

def _sleep(attempt: int) -> None:
    # exponential backoff + jitter
    time.sleep(min((1.7 ** attempt) + random.uniform(0.0, 0.6), 20.0))

def http_json(url: str, method: str, headers: dict, body_obj=None, timeout: int = 30, retries: int = 5):
    data = None
    if body_obj is not None:
        data = json.dumps(body_obj).encode("utf-8")

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
            raw = ""
            try:
                raw = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass

            last_err = f"HTTP {e.code} {e.reason}: {raw}"
            # Retry only transient errors
            if e.code in (408, 425, 429, 500, 502, 503, 504):
                _sleep(attempt)
                continue

            # Non-transient: raise with body included
            raise Exception(last_err)
        except Exception as e:
            last_err = str(e)
            _sleep(attempt)

    raise Exception(f"HTTP request failed after retries. Last error: {last_err}")

def notion_headers() -> dict:
    if not NOTION_TOKEN:
        die("Missing NOTION_TOKEN env var.")
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }

def parse_wallets(raw: str) -> list[str]:
    if not raw:
        return []
    parts = [p.strip() for p in raw.replace("\n", ",").split(",")]
    out, seen = [], set()
    for p in parts:
        if not p:
            continue
        if len(p) < 32 or len(p) > 60:
            continue
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out

def rpc_post(payload: dict) -> dict:
    return http_json(
        url=SOLANA_RPC_URL,
        method="POST",
        headers={"Content-Type": "application/json"},
        body_obj=payload,
        timeout=RPC_TIMEOUT_SECS,
        retries=RPC_RETRIES,
    )

def rpc_get_total_sol(wallets: list[str]) -> float:
    total_lamports = 0
    chunk_size = 100
    for i in range(0, len(wallets), chunk_size):
        chunk = wallets[i:i + chunk_size]
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getMultipleAccounts",
            "params": [chunk, {"encoding": "base64"}],
        }
        res = rpc_post(payload)
        if "error" in res:
            raise Exception(f"Solana RPC error: {res['error']}")
        vals = res.get("result", {}).get("value")
        if vals is None:
            raise Exception(f"Unexpected RPC response: {res}")

        for acct in vals:
            if acct and isinstance(acct, dict):
                lamports = acct.get("lamports", 0)
                if isinstance(lamports, int):
                    total_lamports += lamports

        time.sleep(0.15)

    return total_lamports / 1e9

def notion_get_database(db_id: str) -> dict:
    url = f"https://api.notion.com/v1/databases/{db_id}"
    return http_json(
        url=url,
        method="GET",
        headers=notion_headers(),
        body_obj=None,
        timeout=NOTION_TIMEOUT_SECS,
        retries=NOTION_RETRIES,
    )

def notion_query_today_page(db_id: str, date_prop: str, yyyy_mm_dd: str) -> dict | None:
    url = f"https://api.notion.com/v1/databases/{db_id}/query"
    payload = {
        "filter": {"property": date_prop, "date": {"equals": yyyy_mm_dd}},
        "page_size": 1,
    }
    res = http_json(
        url=url,
        method="POST",
        headers=notion_headers(),
        body_obj=payload,
        timeout=NOTION_TIMEOUT_SECS,
        retries=NOTION_RETRIES,
    )
    results = res.get("results", [])
    return results[0] if results else None

def notion_get_number_prop(page: dict, prop_name: str):
    p = page.get("properties", {}).get(prop_name)
    if not p:
        return None
    if p.get("type") == "number":
        return p.get("number")
    return None

def notion_update_page_number(page_id: str, prop_name: str, number_value: float):
    url = f"https://api.notion.com/v1/pages/{page_id}"
    payload = {"properties": {prop_name: {"number": number_value}}}
    return http_json(
        url=url,
        method="PATCH",
        headers=notion_headers(),
        body_obj=payload,
        timeout=NOTION_TIMEOUT_SECS,
        retries=NOTION_RETRIES,
    )

def notion_create_page_daily(db_id: str, title_prop: str, title_text: str,
                            date_prop: str, yyyy_mm_dd: str,
                            baseline_prop: str, baseline_value: float):
    url = "https://api.notion.com/v1/pages"
    payload = {
        "parent": {"database_id": db_id},
        "properties": {
            title_prop: {"title": [{"type": "text", "text": {"content": title_text}}]},
            date_prop: {"date": {"start": yyyy_mm_dd}},
            baseline_prop: {"number": baseline_value},
        },
    }
    return http_json(
        url=url,
        method="POST",
        headers=notion_headers(),
        body_obj=payload,
        timeout=NOTION_TIMEOUT_SECS,
        retries=NOTION_RETRIES,
    )

def today_yyyy_mm_dd() -> str:
    return datetime.now(timezone.utc).date().isoformat()

# -----------------------------
# Main
# -----------------------------
def main():
    if not NOTION_DB_DAILYTOTAL:
        die("Missing NOTION_DB_DAILYTOTAL env var.")

    wallets = parse_wallets(WALLETS_CSV)
    if not wallets:
        die("No wallets found in WALLETS_CSV.")

    # 0) Validate Notion DB schema BEFORE doing anything
    db = notion_get_database(NOTION_DB_DAILYTOTAL)
    props = db.get("properties", {})
    if BASELINE_PROP not in props:
        available = ", ".join(sorted(props.keys()))
        die(
            f"Notion DB does NOT have a property named exactly '{BASELINE_PROP}'.\n"
            f"Available properties in this DB are:\n{available}\n\n"
            f"Fix: rename the Notion column to '{BASELINE_PROP}' OR set a GitHub secret BASELINE_PROP to the exact column name."
        )

    baseline_type = props[BASELINE_PROP].get("type")
    if baseline_type != "number":
        die(
            f"Notion property '{BASELINE_PROP}' exists but is type '{baseline_type}', not 'number'.\n"
            f"Fix: change '{BASELINE_PROP}' column type to Number in Notion."
        )

    yyyy_mm_dd = today_yyyy_mm_dd()

    # 1) Find today's page
    today_page = notion_query_today_page(NOTION_DB_DAILYTOTAL, DATE_PROP, yyyy_mm_dd)

    # 2) If exists and already set -> skip
    if today_page:
        page_id = today_page.get("id")
        existing_baseline = notion_get_number_prop(today_page, BASELINE_PROP)
        if existing_baseline is not None:
            print(f"Baseline already set for {yyyy_mm_dd}: {existing_baseline}. Skipping.")
            return

        # 3) Compute baseline and set it only
        total_sol = rpc_get_total_sol(wallets)
        total_sol_rounded = round(float(total_sol), 2)

        notion_update_page_number(page_id, BASELINE_PROP, total_sol_rounded)
        print(f"Set SOL Baseline for {yyyy_mm_dd} to {total_sol_rounded} (updated existing page).")
        return

    # 4) Create minimal page if missing
    total_sol = rpc_get_total_sol(wallets)
    total_sol_rounded = round(float(total_sol), 2)
    title_text = f"{total_sol_rounded:.2f} SOL"

    notion_create_page_daily(
        db_id=NOTION_DB_DAILYTOTAL,
        title_prop=TITLE_PROP_DAILY,
        title_text=title_text,
        date_prop=DATE_PROP,
        yyyy_mm_dd=yyyy_mm_dd,
        baseline_prop=BASELINE_PROP,
        baseline_value=total_sol_rounded,
    )
    print(f"Created today's page {yyyy_mm_dd} with SOL Baseline {total_sol_rounded}.")

if __name__ == "__main__":
    main()
