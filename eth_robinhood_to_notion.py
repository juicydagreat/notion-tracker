#!/usr/bin/env python3
"""
Robinhood Chain (ETH L2) → Notion daily balance tracker.
Zero external dependencies — pure Python stdlib.

Mirrors sol_multi_to_notion.py but reads native ETH balances on
Robinhood Chain (an Ethereum Layer-2, chain id 4663) instead of SOL on
Solana. Optionally also tracks an ERC-20 stablecoin (defaults to
Global Dollar / USDG, the Robinhood-ecosystem stablecoin).

All wallet addresses are masked in logs (0x1234...abcd) so this script
is safe to run on a public GitHub repository.

Writes to the SAME Notion databases as the SOL tracker:
  End Balance / Delta           <- native ETH
  USDC End Balance / USDC Delta <- stablecoin (USDG by default)

Required secrets:
  NOTION_TOKEN, NOTION_DB_PERWALLET, NOTION_DB_DAILYTOTAL,
  ETH_WALLETS_CSV, TITLE_PROP_PERWALLET

Optional (have working defaults for Robinhood Chain):
  RH_PRIMARY_RPC, RH_FALLBACK_RPC,
  STABLE_CONTRACT, STABLE_DECIMALS, STABLE_SYMBOL

RPC notes:
  The public Robinhood Chain RPC rejects requests without a User-Agent
  header (returns 403), so every request sends one. Native balance comes
  from eth_getBalance; the stablecoin comes from an eth_call to the
  ERC-20 balanceOf(address) selector.
"""
import os, sys, json, time, random, re
import urllib.request, urllib.error
from datetime import datetime, timezone

# ── Config ──────────────────────────────────────────────────────────────────────────────
NOTION_TOKEN         = os.environ["NOTION_TOKEN"].strip()
NOTION_DB_PERWALLET  = os.environ["NOTION_DB_PERWALLET"].strip()
NOTION_DB_DAILYTOTAL = os.environ["NOTION_DB_DAILYTOTAL"].strip()
WALLETS_CSV          = os.environ["ETH_WALLETS_CSV"]
TITLE_PROP           = os.environ.get("TITLE_PROP_PERWALLET", "Wallet").strip()
NOTION_VERSION       = "2022-06-28"

# Stablecoin (ERC-20) — defaults to Global Dollar (USDG) on Robinhood Chain.
STABLE_CONTRACT = os.environ.get("STABLE_CONTRACT", "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168").strip()
STABLE_DECIMALS = int(os.environ.get("STABLE_DECIMALS", "6"))
STABLE_SYMBOL   = os.environ.get("STABLE_SYMBOL", "USDG").strip()

RPC_TIMEOUT      = int(os.environ.get("RPC_TIMEOUT",       "30"))
RPC_RETRIES      = int(os.environ.get("RPC_RETRIES",       "5"))
RPC_BACKOFF_CAP  = float(os.environ.get("RPC_BACKOFF_CAP",  "30.0"))
BATCH_SIZE       = int(os.environ.get("BATCH_SIZE",         "10"))
BATCH_PAUSE      = float(os.environ.get("BATCH_PAUSE",      "1.0"))
CALL_DELAY       = float(os.environ.get("CALL_DELAY",       "0.5"))

_rpc_primary  = os.environ.get("RH_PRIMARY_RPC",  "https://rpc.mainnet.chain.robinhood.com").strip()
_rpc_fallback = os.environ.get("RH_FALLBACK_RPC", "").strip()
RPC_URLS = list(dict.fromkeys([u for u in (_rpc_primary, _rpc_fallback) if u]))

USER_AGENT = os.environ.get("RPC_USER_AGENT", "notion-tracker/1.0 (+https://github.com)").strip()

# EVM address: 0x followed by 40 hex chars.
ADDR_RE = re.compile(r"\b0x[0-9a-fA-F]{40}\b")

WEI_PER_ETH = 10 ** 18
# keccak256("balanceOf(address)")[:4]
BALANCEOF_SELECTOR = "0x70a08231"


# ── Helpers ─────────────────────────────────────────────────────────────────────────────
def fail(msg):  print(f"ERROR: {msg}", flush=True); sys.exit(1)
def log(msg):   print(msg, flush=True)
def r2(x):      return None if x is None else round(float(x), 2)
def r6(x):      return None if x is None else round(float(x), 6)
def mask(addr): return f"{addr[:6]}...{addr[-4:]}" if len(addr) >= 10 else addr


def parse_wallets(raw):
    seen, out = set(), []
    for w in ADDR_RE.findall(raw or ""):
        key = w.lower()
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


def backoff(attempt):
    d = min(2 ** attempt + random.uniform(0, 0.8), RPC_BACKOFF_CAP)
    log(f"  [backoff] {d:.1f}s")
    time.sleep(d)


def chunks(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def hex_to_int(h):
    if h is None:
        return 0
    if isinstance(h, str):
        return int(h, 16) if h.startswith("0x") else int(h)
    return int(h)


# ── RPC ────────────────────────────────────────────────────────────────────────────────
_RETRY_CODES    = {408, 425, 429, 500, 502, 503, 504}
_NEXT_URL_CODES = {401, 403}


def _http_post(url, payload):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=RPC_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8", errors="replace") or "{}")


def rpc_call(payload):
    last_err = None
    for url_idx, url in enumerate(RPC_URLS):
        if url_idx > 0:
            log(f"  [fallback] switching to {url}")
        skip_to_next = False
        for attempt in range(RPC_RETRIES):
            try:
                data = _http_post(url, payload)
                if isinstance(data, dict) and data.get("error"):
                    raise Exception(f"RPC error: {data['error']}")
                return data
            except urllib.error.HTTPError as e:
                try:    detail = e.read().decode()
                except: detail = ""
                last_err = f"HTTP {e.code}: {detail}"
                if e.code in _NEXT_URL_CODES:
                    log(f"  [{e.code}] blocked on {url}, trying next endpoint")
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


def _resolve_batch(resp):
    """Turn a JSON-RPC batch response (list) into an id->result dict."""
    if not isinstance(resp, list):
        raise Exception(f"Expected list from batch RPC, got: {type(resp)}")
    out = {}
    for item in resp:
        if item.get("error"):
            raise Exception(f"batch error #{item.get('id')}: {item['error']}")
        out[item["id"]] = item.get("result")
    return out


def get_eth_balances(wallets):
    """Native ETH balance for each wallet (in ETH)."""
    results = {}
    indexed = list(enumerate(wallets))
    for i, chunk in enumerate(chunks(indexed, BATCH_SIZE)):
        if i > 0:
            time.sleep(BATCH_PAUSE)
        batch = [
            {"jsonrpc": "2.0", "id": idx, "method": "eth_getBalance",
             "params": [w, "latest"]}
            for idx, w in chunk
        ]
        resolved = _resolve_batch(rpc_call(batch))
        for idx, _ in chunk:
            if idx not in resolved:
                raise Exception(f"eth_getBalance missing id {idx}")
            results[idx] = hex_to_int(resolved[idx]) / WEI_PER_ETH
    return [results[i] for i in range(len(wallets))]


def _balanceof_data(wallet):
    return BALANCEOF_SELECTOR + wallet.lower().replace("0x", "").rjust(64, "0")


def get_stable_balances(wallets):
    """ERC-20 stablecoin balance for each wallet (in token units)."""
    if not STABLE_CONTRACT:
        return [0.0 for _ in wallets]
    scale = 10 ** STABLE_DECIMALS
    results = {}
    indexed = list(enumerate(wallets))
    for i, chunk in enumerate(chunks(indexed, BATCH_SIZE)):
        if i > 0:
            time.sleep(BATCH_PAUSE)
        batch = [
            {"jsonrpc": "2.0", "id": idx, "method": "eth_call",
             "params": [{"to": STABLE_CONTRACT, "data": _balanceof_data(w)}, "latest"]}
            for idx, w in chunk
        ]
        resolved = _resolve_batch(rpc_call(batch))
        for idx, _ in chunk:
            raw = resolved.get(idx)
            # Empty result (0x) means no token account / zero balance.
            results[idx] = hex_to_int(raw) / scale if raw and raw != "0x" else 0.0
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
    rows = notion_query_paginated(NOTION_DB_PERWALLET, {
        "filter": {"property": "Date", "date": {"before": today}},
        "sorts":  [{"property": "Date", "direction": "descending"}],
        "page_size": 100,
    })
    lookup = {}
    for page in rows:
        try:
            w = page["properties"][TITLE_PROP]["title"][0]["plain_text"]
            if w not in lookup:
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
    return (res.get("results") or [None])[0]


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
    log("Robinhood Chain (ETH L2) → Notion tracker")
    log(f"RPC: {RPC_URLS}")
    log(f"Stablecoin: {STABLE_SYMBOL} {mask(STABLE_CONTRACT)} (decimals={STABLE_DECIMALS})")

    wallets = parse_wallets(WALLETS_CSV)
    if not wallets:
        fail("No valid 0x EVM addresses found in ETH_WALLETS_CSV")

    today = datetime.now(timezone.utc).date().isoformat()
    log(f"Date: {today}  |  Wallets: {len(wallets)}")

    log(f"\n--- Fetching native ETH balances ({len(wallets)} wallets) ---")
    eth_list = get_eth_balances(wallets)
    total_eth = r6(sum(eth_list))
    log(f"Total ETH: {total_eth}")

    time.sleep(CALL_DELAY)

    log(f"\n--- Fetching {STABLE_SYMBOL} balances ---")
    stable_list = get_stable_balances(wallets)
    total_stable = r2(sum(stable_list))
    log(f"Total {STABLE_SYMBOL}: {total_stable}")

    log(f"\nSummary: Total ETH={total_eth}  Total {STABLE_SYMBOL}={total_stable}")

    log(f"\n--- Fetching previous Notion rows ---")
    prev_lookup = get_prev_perwallet_rows(today)
    prev_total  = get_prev_total_row(today)
    log(f"  {len(prev_lookup)} previous per-wallet rows found")

    log(f"\n--- Writing {len(wallets)} per-wallet rows to Notion ---")
    for i, (w, eth, stable) in enumerate(zip(wallets, eth_list, stable_list), 1):
        eth    = r6(eth)
        stable = r2(stable)
        prev     = prev_lookup.get(w)
        d_eth    = r6(eth    - get_num(prev, "End Balance"))      if prev else None
        d_stable = r2(stable - get_num(prev, "USDC End Balance")) if prev else None
        log(f"  [{i:02d}/{len(wallets)}] {mask(w)}  ETH={eth} Δ{d_eth}  {STABLE_SYMBOL}={stable} Δ{d_stable}")
        create_page(NOTION_DB_PERWALLET, {
            TITLE_PROP:         {"title": [{"text": {"content": w}}]},
            "Date":             {"date":  {"start": today}},
            "End Balance":      {"number": eth},
            "Delta":            {"number": d_eth},
            "USDC End Balance": {"number": stable},
            "USDC Delta":       {"number": d_stable},
        })

    log(f"\n--- Writing daily total row ---")
    p_eth_t    = get_num(prev_total, "End Balance")
    p_stable_t = get_num(prev_total, "USDC End Balance")
    d_eth_t    = r6(total_eth    - p_eth_t)    if prev_total else None
    d_stable_t = r2(total_stable - p_stable_t) if prev_total else None
    log(f"  ETH={total_eth} Δ{d_eth_t}  {STABLE_SYMBOL}={total_stable} Δ{d_stable_t}")
    create_page(NOTION_DB_DAILYTOTAL, {
        "Name":             {"title": [{"text": {"content": f"{total_eth:.4f} ETH"}}]},
        "Date":             {"date":  {"start": today}},
        "End Balance":      {"number": total_eth},
        "Delta":            {"number": d_eth_t},
        "USDC End Balance": {"number": total_stable},
        "USDC Delta":       {"number": d_stable_t},
    })
    log("\nDone.")


if __name__ == "__main__":
    main()
