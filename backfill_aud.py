#!/usr/bin/env python3
"""
Backfill AUD columns on ALL existing tiles — SOL, ETH, and the combined
AUD total — using historical prices.  Supersedes backfill_sol_aud.py.

Pure stdlib (+ local aud_prices).

Does three things:

  1. Per-wallet rows: sets 'Price AUD' + 'AUD Delta' on each row, choosing
     the coin by the wallet address (0x… → ETH, base58 → SOL).
  2. Daily-total SOL/ETH rows: sets 'Price AUD' + 'AUD Delta' (Delta × price).
  3. Combined AUD rows: for every date that has a SOL and/or ETH total,
     computes the whole-portfolio AUD value (SOL+USDC+ETH+USDT at that
     date's prices) and writes/updates an 'AUD' daily-total row with
     'AUD Value' + 'AUD Delta'.

All prices come from ONE bulk daily-series call per coin (last ~365 days).

Idempotent: safe to re-run. It overwrites the AUD columns so the delta
chain stays consistent; existing AUD rows are updated in place, not
duplicated.

Required: NOTION_TOKEN, NOTION_DB_PERWALLET, NOTION_DB_DAILYTOTAL
Optional: coin ids, labels, COINGECKO_API_KEY, HISTORY_DAYS, ROW_PAUSE
"""
import os, sys, re, json, time
import urllib.request, urllib.error

import aud_prices

NOTION_TOKEN         = os.environ["NOTION_TOKEN"].strip()
NOTION_DB_PERWALLET  = os.environ["NOTION_DB_PERWALLET"].strip()
NOTION_DB_DAILYTOTAL = os.environ["NOTION_DB_DAILYTOTAL"].strip()
NOTION_VERSION       = "2022-06-28"

DAILYTOTAL_TITLE = os.environ.get("TITLE_PROP_DAILYTOTAL", "Name").strip()
PERWALLET_TITLE  = os.environ.get("TITLE_PROP_PERWALLET", "Wallet").strip()

SOL_LABEL = os.environ.get("SOL_LABEL", "SOL").strip()
ETH_LABEL = os.environ.get("ETH_LABEL", "ETH").strip()
AUD_LABEL = os.environ.get("AUD_LABEL", "AUD").strip()

SOL_COIN_ID        = os.environ.get("SOL_COIN_ID", "solana").strip()
ETH_COIN_ID        = os.environ.get("ETH_COIN_ID", "ethereum").strip()
SOL_STABLE_COIN_ID = os.environ.get("SOL_STABLE_COIN_ID", "usd-coin").strip()
ETH_STABLE_COIN_ID = os.environ.get("ETH_STABLE_COIN_ID", "tether").strip()

PRICE_PROP     = os.environ.get("PRICE_PROP", "Price AUD").strip()
AUD_DELTA_PROP = os.environ.get("AUD_DELTA_PROP", "AUD Delta").strip()
AUD_VALUE_PROP = os.environ.get("AUD_VALUE_PROP", "AUD Value").strip()
DELTA_PROP     = os.environ.get("DELTA_PROP", "Delta").strip()

HISTORY_DAYS = int(os.environ.get("HISTORY_DAYS", "365"))
ROW_PAUSE    = float(os.environ.get("ROW_PAUSE", "0.34"))

ADDR_0X   = re.compile(r"^0x[0-9a-fA-F]{40}$")
BASE58_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def log(msg): print(msg, flush=True)
def r2(x):    return None if x is None else round(float(x), 2)


def headers():
    return {"Authorization": f"Bearer {NOTION_TOKEN}",
            "Content-Type": "application/json", "Notion-Version": NOTION_VERSION}


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
    if missing:
        log(f"  Adding columns to {db_id[:8]}...: {missing}")
        notion_req(f"https://api.notion.com/v1/databases/{db_id}",
                   {"properties": {n: {"number": {"format": number_format}} for n in missing}},
                   method="PATCH")


def query_all(db_id):
    rows, cursor = [], None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        res = notion_req(f"https://api.notion.com/v1/databases/{db_id}/query", body)
        rows.extend(res.get("results", []))
        if not res.get("has_more"):
            break
        cursor = res.get("next_cursor")
    return rows


def pnum(page, prop):
    try:
        v = page["properties"][prop]["number"]
        return float(v) if v is not None else None
    except (KeyError, TypeError):
        return None


def ptitle(page, prop):
    try:
        return page["properties"][prop]["title"][0]["plain_text"]
    except (KeyError, IndexError, TypeError):
        return ""


def pdate(page):
    try:
        return page["properties"]["Date"]["date"]["start"][:10]
    except (KeyError, TypeError):
        return None


def patch(page_id, props):
    notion_req(f"https://api.notion.com/v1/pages/{page_id}", {"properties": props}, method="PATCH")


def main():
    log("=" * 60)
    log("Backfill AUD — all tiles (SOL / ETH / combined)")

    # One bulk daily series per coin (spaced out to stay under the free-tier
    # rate limit; _get still retries on 429 as a safety net).
    log("Fetching price history (AUD)...")
    series = {}
    for coin in dict.fromkeys([SOL_COIN_ID, ETH_COIN_ID, SOL_STABLE_COIN_ID, ETH_STABLE_COIN_ID]):
        series[coin] = aud_prices.daily_series_aud(coin, HISTORY_DAYS)
        time.sleep(2.0)
    price = lambda coin, d: aud_prices.price_on(series[coin], d)

    ensure_number_props(NOTION_DB_PERWALLET,  [PRICE_PROP, AUD_DELTA_PROP])
    ensure_number_props(NOTION_DB_DAILYTOTAL, [PRICE_PROP, AUD_DELTA_PROP, AUD_VALUE_PROP])

    # ---- 1. Per-wallet rows (asset chosen by address) ----
    log("\n=== Per-wallet rows ===")
    pw = query_all(NOTION_DB_PERWALLET)
    done = skip = 0
    for page in pw:
        addr = ptitle(page, PERWALLET_TITLE)
        date = pdate(page)
        if ADDR_0X.match(addr):
            coin = ETH_COIN_ID
        elif BASE58_RE.match(addr):
            coin = SOL_COIN_ID
        else:
            skip += 1
            continue
        p = r2(price(coin, date)) if date else None
        if p is None:
            skip += 1
            continue
        delta = pnum(page, DELTA_PROP)
        patch(page["id"], {
            PRICE_PROP:     {"number": p},
            AUD_DELTA_PROP: {"number": r2(delta * p) if delta is not None else None},
        })
        done += 1
        time.sleep(ROW_PAUSE)
    log(f"  per-wallet: updated={done} skipped={skip}")

    # ---- 2 & 3. Daily-total rows ----
    log("\n=== Daily-total rows ===")
    rows = query_all(NOTION_DB_DAILYTOTAL)
    sol_by_date, eth_by_date, aud_by_date = {}, {}, {}
    for page in rows:
        title = ptitle(page, DAILYTOTAL_TITLE)
        date = pdate(page)
        if not date:
            continue
        if AUD_LABEL in title:
            aud_by_date[date] = page
        elif SOL_LABEL in title:
            sol_by_date[date] = page
        elif ETH_LABEL in title:
            eth_by_date[date] = page

    # 2. SOL/ETH tiles: Price AUD + AUD Delta
    for label, by_date, coin in [(SOL_LABEL, sol_by_date, SOL_COIN_ID),
                                 (ETH_LABEL, eth_by_date, ETH_COIN_ID)]:
        done = 0
        for date, page in sorted(by_date.items()):
            p = r2(price(coin, date))
            if p is None:
                continue
            delta = pnum(page, DELTA_PROP)
            patch(page["id"], {
                PRICE_PROP:     {"number": p},
                AUD_DELTA_PROP: {"number": r2(delta * p) if delta is not None else None},
            })
            done += 1
            time.sleep(ROW_PAUSE)
        log(f"  {label} tiles: updated={done}")

    # 3. Combined AUD tiles per date
    log("  combined AUD tiles:")
    all_dates = sorted(set(sol_by_date) | set(eth_by_date))
    prev_total = None
    created = updated = noprice = 0
    for date in all_dates:
        pS, pE = price(SOL_COIN_ID, date), price(ETH_COIN_ID, date)
        pUSDC, pUSDT = price(SOL_STABLE_COIN_ID, date), price(ETH_STABLE_COIN_ID, date)
        if None in (pS, pE, pUSDC, pUSDT):
            noprice += 1
            continue
        s, e = sol_by_date.get(date), eth_by_date.get(date)
        sol_amt  = pnum(s, "End Balance") or 0.0 if s else 0.0
        usdc_amt = pnum(s, "USDC End Balance") or 0.0 if s else 0.0
        eth_amt  = pnum(e, "End Balance") or 0.0 if e else 0.0
        usdt_amt = pnum(e, "USDC End Balance") or 0.0 if e else 0.0
        total = r2(sol_amt * pS + usdc_amt * pUSDC + eth_amt * pE + usdt_amt * pUSDT)
        delta = r2(total - prev_total) if prev_total is not None else None
        prev_total = total

        props = {
            DAILYTOTAL_TITLE: {"title": [{"text": {"content": f"{total:,.2f} {AUD_LABEL}"}}]},
            "Date":           {"date": {"start": date}},
            AUD_VALUE_PROP:   {"number": total},
            AUD_DELTA_PROP:   {"number": delta},
        }
        if date in aud_by_date:
            patch(aud_by_date[date]["id"], props)
            updated += 1
        else:
            notion_req("https://api.notion.com/v1/pages",
                       {"parent": {"database_id": NOTION_DB_DAILYTOTAL}, "properties": props})
            created += 1
        log(f"    {date}  total={total:,.2f} Δ={delta}")
        time.sleep(ROW_PAUSE)
    log(f"  combined AUD: created={created} updated={updated} no_price={noprice}")
    log("\nDone.")


if __name__ == "__main__":
    main()
