#!/usr/bin/env python3
"""
Combined AUD total → Notion.  Third daily-total tile.

Reads the most recent SOL and ETH daily-total rows this tracker already
writes, converts every asset to AUD (SOL, ETH, USDC, USDT), and writes a
single combined row:

  Name        "<total> AUD"        (title — the tile heading)
  AUD Value   total AUD, all assets
  AUD Delta   change vs the previous AUD row

Rows are isolated in the shared daily-total DB by AUD_LABEL ("AUD"), the
same way the SOL/ETH trackers isolate theirs — so the three series never
cross-contaminate. Run this AFTER both trackers (later cron).

Pure stdlib (+ local aud_prices).

Required: NOTION_TOKEN, NOTION_DB_DAILYTOTAL
Optional: DAILYTOTAL_TITLE (default Name), AUD_LABEL (default AUD),
          SOL_LABEL (SOL), ETH_LABEL (ETH),
          SOL_COIN_ID (solana), ETH_COIN_ID (ethereum),
          SOL_STABLE_COIN_ID (usd-coin), ETH_STABLE_COIN_ID (tether),
          COINGECKO_API_KEY
"""
import os, sys, json
import urllib.request, urllib.error
from datetime import datetime, timezone

import aud_prices

NOTION_TOKEN         = os.environ["NOTION_TOKEN"].strip()
NOTION_DB_DAILYTOTAL = os.environ["NOTION_DB_DAILYTOTAL"].strip()
NOTION_VERSION       = "2022-06-28"

DAILYTOTAL_TITLE = os.environ.get("TITLE_PROP_DAILYTOTAL", "Name").strip()
AUD_LABEL        = os.environ.get("AUD_LABEL", "AUD").strip()
SOL_LABEL        = os.environ.get("SOL_LABEL", "SOL").strip()
ETH_LABEL        = os.environ.get("ETH_LABEL", "ETH").strip()

SOL_COIN_ID        = os.environ.get("SOL_COIN_ID", "solana").strip()
ETH_COIN_ID        = os.environ.get("ETH_COIN_ID", "ethereum").strip()
SOL_STABLE_COIN_ID = os.environ.get("SOL_STABLE_COIN_ID", "usd-coin").strip()
ETH_STABLE_COIN_ID = os.environ.get("ETH_STABLE_COIN_ID", "tether").strip()

AUD_VALUE_PROP = os.environ.get("AUD_VALUE_PROP", "AUD Value").strip()
AUD_DELTA_PROP = os.environ.get("AUD_DELTA_PROP", "AUD Delta").strip()


def log(msg): print(msg, flush=True)
def r2(x):    return None if x is None else round(float(x), 2)


def headers():
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Content-Type": "application/json",
        "Notion-Version": NOTION_VERSION,
    }


def notion_req(url, body=None, method="POST"):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers(), method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            res = json.loads(r.read().decode("utf-8", errors="replace") or "{}")
            if isinstance(res, dict) and res.get("object") == "error":
                raise Exception(f"Notion error: {res}")
            return res
    except urllib.error.HTTPError as e:
        raise Exception(f"Notion HTTP {e.code}: {e.read().decode()}")


def ensure_number_props(db_id, names, number_format="australian_dollar"):
    db = notion_req(f"https://api.notion.com/v1/databases/{db_id}", method="GET")
    existing = set(db.get("properties", {}).keys())
    missing = [n for n in names if n not in existing]
    if not missing:
        return
    log(f"  Adding columns: {missing}")
    props = {n: {"number": {"format": number_format}} for n in missing}
    notion_req(f"https://api.notion.com/v1/databases/{db_id}", {"properties": props}, method="PATCH")


def latest_row(label, before=None):
    """Most recent daily-total row whose title contains `label`."""
    date_filter = {"property": "Date", "date": {"before": before}} if before \
        else {"property": "Date", "date": {"is_not_empty": True}}
    res = notion_req(
        f"https://api.notion.com/v1/databases/{NOTION_DB_DAILYTOTAL}/query",
        {
            "filter": {"and": [
                date_filter,
                {"property": DAILYTOTAL_TITLE, "title": {"contains": label}},
            ]},
            "sorts": [{"property": "Date", "direction": "descending"}],
            "page_size": 1,
        },
    )
    return (res.get("results") or [None])[0]


def num(page, prop):
    if page is None:
        return 0.0
    try:
        v = page["properties"][prop]["number"]
        return float(v) if v is not None else 0.0
    except (KeyError, TypeError):
        return 0.0


def row_date(page):
    try:
        return page["properties"]["Date"]["date"]["start"][:10]
    except (KeyError, TypeError):
        return "?"


def main():
    today = datetime.now(timezone.utc).date().isoformat()
    log("=" * 60)
    log(f"Combined AUD total  |  {today}")

    sol_row = latest_row(SOL_LABEL)
    eth_row = latest_row(ETH_LABEL)
    if sol_row is None and eth_row is None:
        log("ERROR: no SOL or ETH daily-total rows found — run the trackers first.")
        sys.exit(1)
    if sol_row is None:
        log(f"  WARNING: no {SOL_LABEL} row found — treating SOL/USDC as 0")
    if eth_row is None:
        log(f"  WARNING: no {ETH_LABEL} row found — treating ETH/USDT as 0")

    sol_amt  = num(sol_row, "End Balance")
    usdc_amt = num(sol_row, "USDC End Balance")
    eth_amt  = num(eth_row, "End Balance")
    usdt_amt = num(eth_row, "USDC End Balance")
    log(f"  SOL row ({row_date(sol_row) if sol_row else '-'}): {sol_amt} SOL, {usdc_amt} USDC")
    log(f"  ETH row ({row_date(eth_row) if eth_row else '-'}): {eth_amt} ETH, {usdt_amt} USDT")

    prices = aud_prices.spot_aud([SOL_COIN_ID, ETH_COIN_ID, SOL_STABLE_COIN_ID, ETH_STABLE_COIN_ID])
    p_sol   = prices.get(SOL_COIN_ID) or 0.0
    p_eth   = prices.get(ETH_COIN_ID) or 0.0
    p_usdc  = prices.get(SOL_STABLE_COIN_ID) or 0.0
    p_usdt  = prices.get(ETH_STABLE_COIN_ID) or 0.0
    log(f"  Prices AUD: SOL={p_sol} ETH={p_eth} USDC={p_usdc} USDT={p_usdt}")

    aud_sol  = sol_amt  * p_sol
    aud_usdc = usdc_amt * p_usdc
    aud_eth  = eth_amt  * p_eth
    aud_usdt = usdt_amt * p_usdt
    total_aud = r2(aud_sol + aud_usdc + aud_eth + aud_usdt)
    log(f"  AUD: SOL={r2(aud_sol)} USDC={r2(aud_usdc)} ETH={r2(aud_eth)} USDT={r2(aud_usdt)}")
    log(f"  TOTAL AUD: {total_aud}")

    ensure_number_props(NOTION_DB_DAILYTOTAL, [AUD_VALUE_PROP, AUD_DELTA_PROP])

    prev_aud = latest_row(AUD_LABEL, before=today)
    delta = r2(total_aud - num(prev_aud, AUD_VALUE_PROP)) if prev_aud else None
    log(f"  Previous AUD total: {num(prev_aud, AUD_VALUE_PROP) if prev_aud else None}  ->  Delta: {delta}")

    notion_req(
        "https://api.notion.com/v1/pages",
        {"parent": {"database_id": NOTION_DB_DAILYTOTAL}, "properties": {
            DAILYTOTAL_TITLE: {"title": [{"text": {"content": f"{total_aud:,.2f} {AUD_LABEL}"}}]},
            "Date":           {"date": {"start": today}},
            AUD_VALUE_PROP:   {"number": total_aud},
            AUD_DELTA_PROP:   {"number": delta},
        }},
    )
    log(f"\nWrote combined row: {total_aud:,.2f} {AUD_LABEL}  (Δ {delta})")


if __name__ == "__main__":
    main()
