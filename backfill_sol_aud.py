#!/usr/bin/env python3
"""
One-time backfill — populate 'Price AUD' and 'AUD Delta' on the EXISTING
Solana Notion rows using historical prices for each row's date.

Pure Python stdlib (+ the local aud_prices module).

Walks both the per-wallet and daily-total databases, and for every row:
  - reads its Date and existing Delta (End Balance change),
  - looks up the historical AUD price for that date (CoinGecko),
  - sets  Price AUD = price   and   AUD Delta = Delta * price.

Idempotent: by default rows that already have an AUD Delta are skipped, so
it is safe to re-run. Set FORCE=1 to overwrite every row.

Required secrets:
  NOTION_TOKEN, NOTION_DB_PERWALLET, NOTION_DB_DAILYTOTAL
Optional:
  PRICE_COIN_ID (default solana), PRICE_PROP, AUD_DELTA_PROP,
  DELTA_PROP (default "Delta"), FORCE, ROW_PAUSE, COINGECKO_API_KEY
"""
import os, sys, json, time
import urllib.request, urllib.error

import aud_prices

NOTION_TOKEN         = os.environ["NOTION_TOKEN"].strip()
NOTION_DB_PERWALLET  = os.environ["NOTION_DB_PERWALLET"].strip()
NOTION_DB_DAILYTOTAL = os.environ["NOTION_DB_DAILYTOTAL"].strip()
NOTION_VERSION       = "2022-06-28"

PRICE_COIN_ID  = os.environ.get("PRICE_COIN_ID", "solana").strip()
PRICE_PROP     = os.environ.get("PRICE_PROP", "Price AUD").strip()
AUD_DELTA_PROP = os.environ.get("AUD_DELTA_PROP", "AUD Delta").strip()
DELTA_PROP     = os.environ.get("DELTA_PROP", "Delta").strip()
FORCE          = os.environ.get("FORCE", "").strip() in ("1", "true", "yes")
ROW_PAUSE      = float(os.environ.get("ROW_PAUSE", "0.34"))   # ~3 Notion writes/sec


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
    log(f"  Adding columns to {db_id[:8]}...: {missing}")
    props = {n: {"number": {"format": number_format}} for n in missing}
    notion_req(f"https://api.notion.com/v1/databases/{db_id}", {"properties": props}, method="PATCH")


def query_all(db_id):
    rows, cursor = [], None
    while True:
        body = {"page_size": 100, "sorts": [{"property": "Date", "direction": "ascending"}]}
        if cursor:
            body["start_cursor"] = cursor
        res = notion_req(f"https://api.notion.com/v1/databases/{db_id}/query", body)
        rows.extend(res.get("results", []))
        if not res.get("has_more"):
            break
        cursor = res.get("next_cursor")
    return rows


def prop_number(page, name):
    try:
        v = page["properties"][name]["number"]
        return float(v) if v is not None else None
    except (KeyError, TypeError):
        return None


def prop_date(page, name="Date"):
    try:
        return page["properties"][name]["date"]["start"][:10]
    except (KeyError, TypeError):
        return None


def backfill_db(db_id, label):
    log(f"\n=== Backfilling {label} ({db_id[:8]}...) ===")
    ensure_number_props(db_id, [PRICE_PROP, AUD_DELTA_PROP])
    rows = query_all(db_id)
    log(f"  {len(rows)} rows")

    updated = skipped = no_price = 0
    for i, page in enumerate(rows, 1):
        date = prop_date(page)
        if not date:
            skipped += 1
            continue
        if not FORCE and prop_number(page, AUD_DELTA_PROP) is not None:
            skipped += 1
            continue

        price = r2(aud_prices.historical_aud(PRICE_COIN_ID, date))
        if price is None:
            log(f"  [{i:03d}/{len(rows)}] {date}  no historical price — skipped")
            no_price += 1
            continue

        delta = prop_number(page, DELTA_PROP)
        aud_delta = r2(delta * price) if delta is not None else None

        notion_req(
            f"https://api.notion.com/v1/pages/{page['id']}",
            {"properties": {
                PRICE_PROP:     {"number": price},
                AUD_DELTA_PROP: {"number": aud_delta},
            }},
            method="PATCH",
        )
        log(f"  [{i:03d}/{len(rows)}] {date}  price={price}  Δ={delta}  AUDΔ={aud_delta}")
        updated += 1
        time.sleep(ROW_PAUSE)

    log(f"  Done {label}: updated={updated} skipped={skipped} no_price={no_price}")
    return updated


def main():
    log("=" * 60)
    log(f"Backfill AUD price/delta  |  coin={PRICE_COIN_ID}  FORCE={FORCE}")
    log(f"Columns: '{PRICE_PROP}', '{AUD_DELTA_PROP}'  (delta source: '{DELTA_PROP}')")
    total = 0
    total += backfill_db(NOTION_DB_PERWALLET,  "per-wallet")
    total += backfill_db(NOTION_DB_DAILYTOTAL, "daily-total")
    log(f"\nAll done. {total} rows updated.")


if __name__ == "__main__":
    main()
