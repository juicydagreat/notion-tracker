#!/usr/bin/env python3
"""
sol_multi_to_notion.py

Purpose:
- Compute TOTAL SOL across all wallets in WALLETS_CSV (comma/newline separated)
- Write it into your existing Notion DAILY TOTAL database as a "SOL Baseline" number
- Only writes once per day:
    - If today's page exists AND baseline is already set -> do nothing
    - If today's page exists AND baseline is empty -> set it
    - If today's page does not exist -> create it (minimal) and set baseline

This script intentionally DOES NOT update any other fields.
No external dependencies (no requests). Uses urllib only.
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

# -----------------------------
# Config via env
# -----------------------------
NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "").strip()
NOTION_DB_DAILYTOTAL = os.environ.get("NOTION_DB_DAILYTOTAL", "").strip()

# Your existing Notion property names (defaults match your prior convention)
DATE_PROP = os.environ.get("DATE_PROP", "Date").strip()
TITLE_PROP_DAILY = os.environ.get("TOTAL_TITLE_PROP", "Name").strip()

# The new baseline field (you must add this property in Notion as a Number)
BASELINE_PROP = os.environ.get("BASELINE_PROP", "SOL Baseline").strip()

# Wallet input (secret)
WALLETS_CSV = os.environ.get("WALLETS_CSV", "").strip()

# Solana RPC (secret recommended)
SOLANA_RPC_URL = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com").strip()

# Safety knobs
RPC_TIMEOUT_SECS = int(os.environ.get("RPC_TIMEOUT_SECS", "30"))
RPC_RETRIES = int(os.environ.get("RPC_RETRIES", "6"))
RPC_BACKOFF_BASE = float(os.environ.get("RPC_BACKOFF_BASE", "1.6"))  # exponential
RPC_BACKOFF_JITTER = float(os.environ.get("RPC_BACKOFF_JITTER", "0.25"))

NOTION_TIMEOUT_SECS = int(os.environ.get("NOTION_TIMEOUT_SECS", "30"))
NOTION_RETRIES = int(os.environ.get("NOTION_RETRIES", "5"))
NOTION_BACKOFF_BASE = float(os.environ.get("NOTION_BACKOFF_BASE", "1.6"))
NOTION_BACKOFF_JITTER = float(os.environ.get("NOTION_BACKOFF_JITTER", "0.25"))

NOTION_VERSION = os.environ.get("NOTION_VERSION", "2022-06-28").strip()

# -----------------------------
# Helpers
# -----------------------------
def die(msg: str, code: int = 1) -> None:
    print(msg, file=sys.stderr)
    sys.exit(code)

def _sleep_backoff(attempt: int, base: float, jitter: float) -> None:
    # Exponential backoff with small jitter
    delay = (base ** attempt)
    delay = delay * (1.0 + (jitter * (2.0 * (time.time() % 1.0) - 1.0)))  # deterministic-ish jitter
    time.sleep(max(0.2, min(delay, 30.0)))

def http_json(url: str, method: str, headers: dict, body_obj=None, timeout: int = 30, retries: int = 5,
              backoff_base: float = 1.6, backoff_jitter: float = 0.25):
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
                raw = resp.read().decode("utf-8")
                if not raw:
                    return {}
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            # Try to read JSON error details
            try:
                raw = e.read().decode("utf-8")
            except Exception:
                raw = ""
            last_err = f"HTTP {e.code} {e.reason} {raw}".strip()
            # Retry on common transient codes
            if e.code in (408, 425, 429, 500, 502, 503, 504):
                _sleep_backoff(attempt, backoff_base, backoff_jitter)
                continue
            raise
        except Exception as e:
            last_err = str(e)
            _sleep_backoff(attempt, backoff_base, backoff_jitter)
            continue

    raise Exception(f"HTTP request failed after retries. Last error: {last_err}")

def notion_headers() -> dict:
    if not NOTION_TOKEN:
        die("Missing NOTION_TOKEN env var")
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }

def parse_wallets(raw: str) -> list[str]:
    """
    Accepts comma/newline separated wallet pubkeys.
    Trims whitespace. Removes empty entries. De-dupes while preserving order.
    """
    if not raw:
        return []
    # Replace newlines with commas, split, strip
    parts = [p.strip() for p in raw.replace("\n", ",").split(",")]
    out = []
    seen = set()
    for p in parts:
        if not p:
            continue
        # Basic sanity: Solana pubkeys are base58, usually length 32-44 chars.
        if len(p) < 32 or len(p) > 60:
            # Don't hard fail; just skip obviously broken items
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
        backoff_base=RPC_BACKOFF_BASE,
        backoff_jitter=RPC_BACKOFF_JITTER,
    )

def rpc_get_total_sol(wallets: list[str]) -> float:
    """
    Uses getMultipleAccounts (chunked) to reduce rate limits.
    Returns total SOL across wallets.
    """
    total_lamports = 0

    # getMultipleAccounts supports up to 100 accounts per call commonly
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
        if "result" not in res or "value" not in res["result"]:
            raise Exception(f"Unexpected RPC response: {res}")

        values = res["result"]["value"]
        # Each item can be None (account not found) or dict with 'lamports'
        for acct in values:
            if acct and isinstance(acct, dict):
                lamports = acct.get("lamports", 0)
                if isinstance(lamports, int):
                    total_lamports += lamports

        # Tiny pause to be polite (usually unnecessary with chunking, but safe)
        time.sleep(0.2)

    return total_lamports / 1e9

def notion_query_today_page(db_id: str, date_prop: str, yyyy_mm_dd: str) -> dict | None:
    """
    Query the database for a page where Date == yyyy-mm-dd.
    Returns the first matching page object, else None.
    """
    url = f"https://api.notion.com/v1/databases/{db_id}/query"
    payload = {
        "filter": {
            "property": date_prop,
            "date": {"equals": yyyy_mm_dd},
        },
        "page_size": 1,
    }
    res = http_json(
        url=url,
        method="POST",
        headers=notion_headers(),
        body_obj=payload,
        timeout=NOTION_TIMEOUT_SECS,
        retries=NOTION_RETRIES,
        backoff_base=NOTION_BACKOFF_BASE,
        backoff_jitter=NOTION_BACKOFF_JITTER,
    )
    results = res.get("results", [])
    return results[0] if results else None

def notion_get_number_prop(page: dict, prop_name: str):
    """
    Extract a Notion number property value from a page.
    Returns:
      - float/int value if set
      - None if missing or null
    """
    props = page.get("properties", {})
    p = props.get(prop_name)
    if not p:
        return None
    # number properties look like: {"type":"number","number": 12.34}
    if p.get("type") == "number":
        return p.get("number")
    return None

def notion_update_page_number(page_id: str, prop_name: str, number_value: float):
    url = f"https://api.notion.com/v1/pages/{page_id}"
    payload = {
        "properties": {
            prop_name: {"number": number_value}
        }
    }
    return http_json(
        url=url,
        method="PATCH",
        headers=notion_headers(),
        body_obj=payload,
        timeout=NOTION_TIMEOUT_SECS,
        retries=NOTION_RETRIES,
        backoff_base=NOTION_BACKOFF_BASE,
        backoff_jitter=NOTION_BACKOFF_JITTER,
    )

def notion_create_page_daily(db_id: str, title_prop: str, title_text: str,
                            date_prop: str, yyyy_mm_dd: str,
                            baseline_prop: str, baseline_value: float):
    url = "https://api.notion.com/v1/pages"
    payload = {
        "parent": {"database_id": db_id},
        "properties": {
            title_prop: {
                "title": [{"type": "text", "text": {"content": title_text}}]
            },
            date_prop: {
                "date": {"start": yyyy_mm_dd}
            },
            baseline_prop: {
                "number": baseline_value
            }
        }
    }
    return http_json(
        url=url,
        method="POST",
        headers=notion_headers(),
        body_obj=payload,
        timeout=NOTION_TIMEOUT_SECS,
        retries=NOTION_RETRIES,
        backoff_base=NOTION_BACKOFF_BASE,
        backoff_jitter=NOTION_BACKOFF_JITTER,
    )

def today_yyyy_mm_dd_local() -> str:
    # GitHub runner uses UTC by default; this makes the date stable.
    # If you need AEST date boundaries, schedule your workflow at the right UTC time
    # (which you already do). So storing UTC "today" is fine.
    return datetime.now(timezone.utc).date().isoformat()

# -----------------------------
# Main
# -----------------------------
def main():
    if not NOTION_DB_DAILYTOTAL:
        die("Missing NOTION_DB_DAILYTOTAL env var (your calendar database id).")

    wallets = parse_wallets(WALLETS_CSV)
    if not wallets:
        die("No wallets found in WALLETS_CSV. Provide comma/newline separated pubkeys.")

    yyyy_mm_dd = today_yyyy_mm_dd_local()

    # 1) Find today's page in the existing calendar DB
    today_page = notion_query_today_page(NOTION_DB_DAILYTOTAL, DATE_PROP, yyyy_mm_dd)

    # 2) If page exists and baseline already set -> skip
    if today_page:
        page_id = today_page.get("id")
        existing_baseline = notion_get_number_prop(today_page, BASELINE_PROP)

        if existing_baseline is not None:
            print(f"Baseline already set for {yyyy_mm_dd}: {existing_baseline}. Skipping.")
            return

        # 3) Compute baseline (TOTAL SOL) and set it ONLY (no other changes)
        total_sol = rpc_get_total_sol(wallets)
        total_sol_rounded = round(float(total_sol), 2)

        notion_update_page_number(page_id, BASELINE_PROP, total_sol_rounded)
        print(f"Set SOL Baseline for {yyyy_mm_dd} to {total_sol_rounded} (updated existing page).")
        return

    # 4) No page for today -> create minimal page with baseline
    total_sol = rpc_get_total_sol(wallets)
    total_sol_rounded = round(float(total_sol), 2)

    # Title format kept simple so it looks like your calendar cards.
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
