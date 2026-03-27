#!/usr/bin/env python3
"""
Solana → Notion daily balance tracker.
Zero external dependencies — pure Python stdlib.

HTTP calls per run:
  1  batch getBalance for all wallets (SOL)
  1  getTokenAccountsByOwner for the single USDC wallet
  1  Notion DB query for all previous per-wallet rows
  1  Notion DB query for previous daily total
  N  Notion page creates (one per wallet + 1 daily total)

RPC endpoints (tried in order, first success wins):
  SOLANA_PRIMARY_RPC   default: https://rpc.ankr.com/solana
  SOLANA_FALLBACK_RPC  default: https://solana-rpc.publicnode.com
Both are free public endpoints requiring no API key.
"""
import os, sys, json, time, random, re
import urllib.request, urllib.error
from datetime import datetime, timezone

# ── Config ──────────────────────────────────────────────────────────────────────────────
NOTION_TOKEN         = os.environ["NOTION_TOKEN"].strip()
NOTION_DB_PERWALLET  = os.environ["NOTION_DB_PERWALLET"].strip()
NOTION_DB_DAILYTOTAL = os.environ["NOTION_DB_DAILYTOTAL"].strip()
WALLETS_CSV          = os.environ["WALLETS_CSV"]
USDC_MINT            = os.environ.get("USDC_MINT",   "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v").strip()
USDC_WALLET          = os.environ.get("USDC_WALLET", "33EUErqH7mog7U2XdtXaZL7S1EEpJw1TEv7dswm76SzM").strip()
TITLE_PROP           = os.environ.get("TITLE_PROP_PERWALLET", "Wallet").strip()
NOTION_VERSION       = "2022-06-28"

RPC_TIMEOUT     = int(os.environ.get("RPC_TIMEOUT",     "30"))
RPC_RETRIES     = int(os.environ.get("RPC_RETRIES",     "5"))
RPC_BACKOFF_CAP = float(os.environ.get("RPC_BACKOFF_CAP", "30.0"))
BATCH_SIZE      = int(os.environ.get("BATCH_SIZE",       "50"))
BATCH_PAUSE     = float(os.environ.get("BATCH_PAUSE",    "1.0"))

# RPC endpoints tried in order — renamed vars to avoid clashing with old secrets
_rpc_primary  = os.environ.get("SOLANA_PRIMARY_RPC",  "https://rpc.ankr.com/solana").strip()
_rpc_fallback = os.environ.get("SOLANA_FALLBACK_RPC", "https://solana-rpc.publicnode.com").strip()
RPC_URLS = list(dict.fromkeys([_rpc_primary, _rpc_fallback]))  # dedupe, preserve order

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
# HTTP codes that warrant retrying the same URL
_RETRY_CODES = {408, 425, 429, 500, 502, 503, 504}
# HTTP codes that mean this URL is broken — skip to next immediately
_NEXT_URL_CODES = {401, 403}


def rpc_call(payload):
    """
    POST a single or batch JSON-RPC payload.
    Tries each URL in RPC_URLS in order:
      - 401/403  → bad/missing key, skip to next URL immediately
      - 429/5xx  → retry same URL with backoff
      - success  → return
    """
    headers = {"Content-Type": "application/json"}
    last_err = None

    for url_idx, url in enumerate(RPC_URLS):
        if url_idx > 0:
            log(f"  [fallback] switching to {url}")
        skip_to_next = False
        for attempt in range(RPC_RETRIES):
            try:
                req = urllib.request.Request(
                    url,
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
                if e.code in _NEXT_URL_CODES:
                    log(f"  [{e.code}] bad/missing API key on {url}, trying next endpoint")
                    skip_to_next = True
                    break
                elif e.code in _RETRY_CODES:
                    log(f"  [{e.code}] retrying {url}")
                    backoff(attempt)
                else:
                    raise Exception(last_err)
            except Exception as ex:
                last_err = str(ex)
                log(f"  [rpc] {last_err}")
                backoff(attempt)
        if skip_to_next:
            continue

    raise Exception(f"All RPC endpoints failed. Last error: {last_err}")


def batch_get_sol(wallets):
    """
    Fetch SOL balance for all wallets using batch JSON-RPC.
    Sends wallets in chunks of BATCH_SIZE — typically 1 HTTP call for 54 wallets.
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


def get_usdc_balance(wallet):
    """
    Fetch USDC balance for a single wallet (1 HTTP call).
    USDC is only ever held in one wallet so no batch needed.
    """
    resp = rpc_call({
        "jsonrpc": "2.0", "id": 1,
        "method": "getTokenAccountsByOwner",
        "params": [wallet, {"mint": USDC_MINT}, {"encoding": "jsonParsed"}],
    })
    total = 0.0
    for acc in resp.get("result", {}).get("value", []):
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
    return total


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
    log("=" * 60)
    log(f"RPC endpoints: {RPC_URLS}")

    wallets = parse_wallets(WALLETS_CSV)
    if not wallets:
        fail("No valid Solana pubkeys found in WALLETS_CSV")

    today = datetime.now(timezone.utc).date().isoformat()
    log(f"Date: {today}  |  Wallets: {len(wallets)}")
    log(f"USDC wallet: {USDC_WALLET}")
    for i, w in enumerate(wallets, 1):
        log(f"  {i:02d}. {w}")

    # — 1 batch HTTP call for all SOL balances —
    log(f"\n--- Fetching SOL balances (batch, size={BATCH_SIZE}) ---")
    sol_list = batch_get_sol(wallets)
    total_sol = r2(sum(sol_list))
    for w, s in zip(wallets, sol_list):
        log(f"  {s:>10.4f} SOL  {w}")

    time.sleep(BATCH_PAUSE)

    # — 1 single HTTP call for USDC (one wallet only) —
    log(f"\n--- Fetching USDC balance ({USDC_WALLET}) ---")
    usdc_total = r2(get_usdc_balance(USDC_WALLET))
    log(f"  {usdc_total} USDC")
    usdc_list = [usdc_total if w == USDC_WALLET else 0.0 for w in wallets]

    log(f"\nSummary: Total SOL={total_sol}  Total USDC={usdc_total}")

    # — 1 Notion query for all previous per-wallet rows —
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
    d_usdc_t = r2(usdc_total - p_usdc_t) if prev_total else None
    log(f"  SOL={total_sol} \u0394{d_sol_t}  USDC={usdc_total} \u0394{d_usdc_t}")
    create_page(NOTION_DB_DAILYTOTAL, {
        "Name":             {"title": [{"text": {"content": f"{total_sol:.2f} SOL"}}]},
        "Date":             {"date":  {"start": today}},
        "End Balance":      {"number": total_sol},
        "Delta":            {"number": d_sol_t},
        "USDC End Balance": {"number": usdc_total},
        "USDC Delta":       {"number": d_usdc_t},
    })
    log("\nDone.")


if __name__ == "__main__":
    main()
