#!/usr/bin/env python3
"""
Solana → Notion daily balance tracker.
Zero external dependencies — pure Python stdlib.

Key optimizations vs previous version:
  - Batch RPC: all getBalance + getTokenAccountsByOwner in 2 HTTP requests
    instead of 80+. Eliminates rate-limit issues on the public RPC.
  - Single Notion query for all previous rows instead of one per wallet.
  - USDC checked on every wallet, not just one hardcoded address.
  - No external packages — pip install not required.

Free RPC options (set SOLANA_RPC_URL secret):
  https://api.mainnet-beta.solana.com   (official public, default)
  https://rpc.ankr.com/solana           (Ankr public, more permissive)
"""
import os, sys, json, time, random, re
import urllib.request, urllib.error
from datetime import datetime, timezone

# ── Config ──────────────────────────────────────────────────────────────────────────────
NOTION_TOKEN         = os.environ["NOTION_TOKEN"].strip()
NOTION_DB_PERWALLET  = os.environ["NOTION_DB_PERWALLET"].strip()
NOTION_DB_DAILYTOTAL = os.environ["NOTION_DB_DAILYTOTAL"].strip()
WALLETS_CSV          = os.environ["WALLETS_CSV"]
SOLANA_RPC_URL       = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com").strip()
USDC_MINT            = os.environ.get("USDC_MINT", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v").strip()
TITLE_PROP           = os.environ.get("TITLE_PROP_PERWALLET", "Wallet").strip()
NOTION_VERSION       = "2022-06-28"

RPC_TIMEOUT     = int(os.environ.get("RPC_TIMEOUT",     "30"))
RPC_RETRIES     = int(os.environ.get("RPC_RETRIES",     "5"))
RPC_BACKOFF_CAP = float(os.environ.get("RPC_BACKOFF_CAP", "30.0"))
# How many wallets per RPC batch call (lower if public RPC still rate-limits)
BATCH_SIZE      = int(os.environ.get("BATCH_SIZE",      "50"))
# Seconds to pause between batch chunks
BATCH_PAUSE     = float(os.environ.get("BATCH_PAUSE",   "1.0"))

PUBKEY_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")


# ── Helpers ─────────────────────────────────────────────────────────────────────────────
def fail(msg):  print(f"ERROR: {msg}", flush=True); sys.exit(1)
def log(msg):   print(msg, flush=True)
def r2(x):      return None if x is None else round(float(x), 2)


def parse_wallets(raw):
    seen, out = set(), []
    for w in PUBKEY_RE.findall(raw or ""):
        if w not in seen:
            seen.add(w)
            out.append(w)
    return out


def backoff(attempt):
    d = min(2 ** attempt + random.uniform(0, 0.8), RPC_BACKOFF_CAP)
    log(f"  [backoff] {d:.1f}s")
    time.sleep(d)


def chunks(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


# ── RPC ────────────────────────────────────────────────────────────────────────────────
def rpc_call(payload):
    """POST a single or batch JSON-RPC payload; retry with exponential backoff."""
    headers = {"Content-Type": "application/json"}
    last_err = None
    for attempt in range(RPC_RETRIES):
        try:
            req = urllib.request.Request(
                SOLANA_RPC_URL,
                data=json.dumps(payload).encode(),
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=RPC_TIMEOUT) as r:
                data = json.loads(r.read().decode("utf-8", errors="replace") or "{}")
            if isinstance(data, dict) and data.get("error"):
                raise Exception(f"RPC error: {data['error']}")
            return data
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode()
            except Exception:
                detail = ""
            last_err = f"HTTP {e.code}: {detail}"
            if e.code == 429:
                log(f"  [429] rate limited")
                if "max usage reached" in detail:
                    raise Exception(f"RPC quota exhausted: {last_err}")
            elif e.code not in (408, 425, 500, 502, 503, 504):
                raise Exception(last_err)
            backoff(attempt)
        except Exception as ex:
            last_err = str(ex)
            log(f"  [rpc] {last_err}")
            backoff(attempt)
    raise Exception(f"RPC failed after {RPC_RETRIES} attempts: {last_err}")


def batch_get_sol(wallets):
    """
    Fetch SOL balance for every wallet using batch JSON-RPC.
    All wallets are sent in chunks of BATCH_SIZE — typically 1–2 HTTP calls
    instead of one call per wallet.
    Returns list of floats in the same order as wallets.
    """
    results = {}
    indexed = list(enumerate(wallets))
    for i, chunk in enumerate(chunks(indexed, BATCH_SIZE)):
        if i > 0:
            time.sleep(BATCH_PAUSE)
        batch = [
            {"jsonrpc": "2.0", "id": idx, "method": "getBalance", "params": [w]}
            for idx, w in chunk
        ]
        resp = rpc_call(batch)
        if not isinstance(resp, list):
            raise Exception(f"Expected batch list from RPC, got: {type(resp)}")
        for item in resp:
            if item.get("error"):
                raise Exception(f"getBalance error for wallet #{item['id']}: {item['error']}")
            results[item["id"]] = item["result"]["value"] / 1e9
    return [results[i] for i in range(len(wallets))]


def batch_get_usdc(wallets):
    """
    Fetch USDC balance for every wallet using batch JSON-RPC.
    Returns list of floats in the same order as wallets.
    """
    results = {}
    indexed = list(enumerate(wallets))
    for i, chunk in enumerate(chunks(indexed, BATCH_SIZE)):
        if i > 0:
            time.sleep(BATCH_PAUSE)
        batch = [
            {
                "jsonrpc": "2.0", "id": idx,
                "method": "getTokenAccountsByOwner",
                "params": [w, {"mint": USDC_MINT}, {"encoding": "jsonParsed"}],
            }
            for idx, w in chunk
        ]
        resp = rpc_call(batch)
        if not isinstance(resp, list):
            raise Exception(f"Expected batch list from RPC, got: {type(resp)}")
        for item in resp:
            if item.get("error"):
                raise Exception(f"getTokenAccountsByOwner error for wallet #{item['id']}: {item['error']}")
            total = 0.0
            for acc in item["result"]["value"]:
                try:
                    ta = acc["account"]["data"]["parsed"]["info"]["tokenAmount"]
                    ui = ta.get("uiAmount")
                    if ui is not None:
                        total += float(ui)
                    else:
                        amt = int(ta.get("amount", 0))
                        dec = int(ta.get("decimals", 0))
                        total += amt / 10 ** dec if dec else float(amt)
                except Exception:
                    continue
            results[item["id"]] = total
    return [results[i] for i in range(len(wallets))]


# ── Notion ─────────────────────────────────────────────────────────────────────────────
def notion_headers():
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Content-Type": "application/json",
        "Notion-Version": NOTION_VERSION,
    }


def notion_req(url, body, method="POST"):
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers=notion_headers(),
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8", errors="replace") or "{}")
            if isinstance(data, dict) and data.get("object") == "error":
                raise Exception(f"Notion error: {data}")
            return data
    except urllib.error.HTTPError as e:
        raise Exception(f"Notion HTTP {e.code}: {e.read().decode()}")


def notion_query_paginated(db_id, body):
    """Fetch all pages of a Notion DB query, handling pagination automatically."""
    rows, cursor = [], None
    while True:
        if cursor:
            body["start_cursor"] = cursor
        res = notion_req(f"https://api.notion.com/v1/databases/{db_id}/query", body)
        rows.extend(res.get("results", []))
        if not res.get("has_more"):
            break
        cursor = res.get("next_cursor")
    return rows


def get_prev_perwallet_rows(today):
    """
    Fetch all per-wallet rows before today in a SINGLE Notion query.
    Returns {wallet_address: most_recent_page} for delta calculations.
    Previously this was one query per wallet (40+ queries).
    """
    rows = notion_query_paginated(NOTION_DB_PERWALLET, {
        "filter": {"property": "Date", "date": {"before": today}},
        "sorts":  [{"property": "Date", "direction": "descending"}],
        "page_size": 100,
    })
    lookup = {}
    for page in rows:
        try:
            w = page["properties"][TITLE_PROP]["title"][0]["plain_text"]
            if w not in lookup:  # first seen = most recent (sorted desc)
                lookup[w] = page
        except (KeyError, IndexError):
            continue
    return lookup


def get_prev_total_row(today):
    res = notion_req(
        f"https://api.notion.com/v1/databases/{NOTION_DB_DAILYTOTAL}/query",
        {
            "filter": {"property": "Date", "date": {"before": today}},
            "sorts":  [{"property": "Date", "direction": "descending"}],
            "page_size": 1,
        },
    )
    results = res.get("results", [])
    return results[0] if results else None


def get_num(page, prop):
    if page is None:
        return 0.0
    try:
        v = page["properties"][prop]["number"]
        return float(v) if v is not None else 0.0
    except (KeyError, TypeError):
        return 0.0


def create_page(db_id, props):
    notion_req(
        "https://api.notion.com/v1/pages",
        {"parent": {"database_id": db_id}, "properties": props},
    )


# ── Main ──────────────────────────────────────────────────────────────────────────────
def main():
    if not SOLANA_RPC_URL.startswith("https://"):
        fail(f"SOLANA_RPC_URL must start with https:// (got: {SOLANA_RPC_URL})")

    wallets = parse_wallets(WALLETS_CSV)
    if not wallets:
        fail("No valid Solana pubkeys found in WALLETS_CSV")

    today = datetime.now(timezone.utc).date().isoformat()

    log("=" * 60)
    log(f"Date: {today}  |  Wallets: {len(wallets)}")
    for i, w in enumerate(wallets, 1):
        log(f"  {i:02d}. {w}")

    # — Fetch all SOL balances: 1 HTTP call (or 2 if >BATCH_SIZE wallets) —
    log(f"\n--- Fetching SOL balances (batch, size={BATCH_SIZE}) ---")
    sol_list = batch_get_sol(wallets)
    total_sol = r2(sum(sol_list))
    for w, s in zip(wallets, sol_list):
        log(f"  {s:>10.4f} SOL  {w}")

    time.sleep(BATCH_PAUSE)

    # — Fetch all USDC balances: 1 HTTP call (or 2 if >BATCH_SIZE wallets) —
    log(f"\n--- Fetching USDC balances (batch, size={BATCH_SIZE}) ---")
    usdc_list = batch_get_usdc(wallets)
    total_usdc = r2(sum(usdc_list))
    for w, u in zip(wallets, usdc_list):
        if u > 0:
            log(f"  {u:>10.2f} USDC  {w}")

    log(f"\nSummary: Total SOL={total_sol}  Total USDC={total_usdc}")

    # — Fetch all previous Notion rows: 1 query instead of 40 —
    log(f"\n--- Fetching previous Notion rows (single query) ---")
    prev_lookup = get_prev_perwallet_rows(today)
    prev_total  = get_prev_total_row(today)
    log(f"  {len(prev_lookup)} previous per-wallet rows found")

    # — Write per-wallet rows —
    log(f"\n--- Writing {len(wallets)} per-wallet rows to Notion ---")
    for w, sol, usdc in zip(wallets, sol_list, usdc_list):
        sol  = r2(sol)
        usdc = r2(usdc)
        prev   = prev_lookup.get(w)
        d_sol  = r2(sol  - get_num(prev, "End Balance"))      if prev else None
        d_usdc = r2(usdc - get_num(prev, "USDC End Balance")) if prev else None
        log(f"  {w}  SOL={sol} \u0394{d_sol}  USDC={usdc} \u0394{d_usdc}")
        create_page(NOTION_DB_PERWALLET, {
            TITLE_PROP:         {"title": [{"text": {"content": w}}]},
            "Date":             {"date":  {"start": today}},
            "End Balance":      {"number": sol},
            "Delta":            {"number": d_sol},
            "USDC End Balance": {"number": usdc},
            "USDC Delta":       {"number": d_usdc},
        })

    # — Write daily total —
    log(f"\n--- Writing daily total row ---")
    p_sol_t  = get_num(prev_total, "End Balance")
    p_usdc_t = get_num(prev_total, "USDC End Balance")
    d_sol_t  = r2(total_sol  - p_sol_t)  if prev_total else None
    d_usdc_t = r2(total_usdc - p_usdc_t) if prev_total else None
    log(f"  SOL={total_sol} \u0394{d_sol_t}  USDC={total_usdc} \u0394{d_usdc_t}")
    create_page(NOTION_DB_DAILYTOTAL, {
        "Name":             {"title": [{"text": {"content": f"{total_sol:.2f} SOL"}}]},
        "Date":             {"date":  {"start": today}},
        "End Balance":      {"number": total_sol},
        "Delta":            {"number": d_sol_t},
        "USDC End Balance": {"number": total_usdc},
        "USDC Delta":       {"number": d_usdc_t},
    })
    log("\nDone.")


if __name__ == "__main__":
    main()
