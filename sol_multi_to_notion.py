import os
import sys
import json
import time
import random
import re
import urllib.request
import urllib.error
from datetime import datetime, timezone

# =========================
# ENV
# =========================
NOTION_TOKEN         = os.environ["NOTION_TOKEN"].strip()
NOTION_DB_PERWALLET  = os.environ["NOTION_DB_PERWALLET"].strip()
NOTION_DB_DAILYTOTAL = os.environ["NOTION_DB_DAILYTOTAL"].strip()
WALLETS_CSV          = os.environ["WALLETS_CSV"]
SOLANA_RPC_URL       = os.environ.get(
    "SOLANA_RPC_URL",
    "https://api.mainnet-beta.solana.com"
).strip()
USDC_WALLET = os.environ.get(
    "USDC_WALLET",
    "33EUErqH7mog7U2XdtXaZL7S1EEpJw1TEv7dswm76SzM"
).strip()
USDC_MINT = os.environ.get(
    "USDC_MINT",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
).strip()
NOTION_VERSION       = os.environ.get("NOTION_VERSION", "2022-06-28").strip()
TITLE_PROP_PERWALLET = os.environ.get("TITLE_PROP_PERWALLET", "Wallet").strip()

RPC_TIMEOUT      = int(os.environ.get("RPC_TIMEOUT",    "30"))
RPC_RETRIES      = int(os.environ.get("RPC_RETRIES",     "8"))
RPC_DELAY_SOL    = float(os.environ.get("RPC_DELAY_SOL",   "0.35"))
RPC_DELAY_USDC   = float(os.environ.get("RPC_DELAY_USDC",  "0.5"))
RPC_BACKOFF_CAP  = float(os.environ.get("RPC_BACKOFF_CAP", "30.0"))

PUBKEY_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")

# =========================
# HELPERS
# =========================
def fail(msg):
    print(f"ERROR: {msg}", flush=True)
    sys.exit(1)

def log(msg):
    print(msg, flush=True)

def r2(x):
    return None if x is None else round(float(x), 2)

def parse_wallets(raw):
    found = PUBKEY_RE.findall(raw or "")
    out = []
    seen = set()
    for w in found:
        if w not in seen:
            seen.add(w)
            out.append(w)
    return out

def assert_pubkey(name, value):
    if not value or not PUBKEY_RE.fullmatch(value):
        fail(f"{name} is not a valid Solana pubkey: '{value}'")

def backoff_sleep(attempt):
    delay = min((2 ** attempt) + random.uniform(0.0, 0.8), RPC_BACKOFF_CAP)
    log(f"  [backoff] sleeping {delay:.2f}s ...")
    time.sleep(delay)

# =========================
# RPC
# =========================
def rpc_post(payload):
    last_err = None
    for attempt in range(RPC_RETRIES):
        try:
            req = urllib.request.Request(
                SOLANA_RPC_URL,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=RPC_TIMEOUT) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                data = json.loads(raw) if raw else {}
                if isinstance(data, dict) and data.get("error") is not None:
                    raise Exception(f"RPC error: {data['error']}")
                return data
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            last_err = f"HTTP {e.code}: {detail}"
            if e.code == 429:
                log(f"  [429] rate limit: {detail}")
                if "max usage reached" in detail:
                    raise Exception(f"RPC quota exhausted: {last_err}")
                backoff_sleep(attempt + 1)
                continue
            if e.code in (408, 425, 500, 502, 503, 504):
                log(f"  [transient {e.code}] retrying ...")
                backoff_sleep(attempt + 1)
                continue
            raise Exception(last_err)
        except Exception as e:
            last_err = str(e)
            log(f"  [rpc error] {last_err}")
            backoff_sleep(attempt + 1)
    raise Exception(f"RPC failed after {RPC_RETRIES} attempts. Last: {last_err}")

def rpc_get_sol_balance(wallet):
    res = rpc_post({
        "jsonrpc": "2.0", "id": 1,
        "method": "getBalance",
        "params": [wallet],
    })
    if "result" not in res or "value" not in res.get("result", {}):
        raise Exception(f"Unexpected getBalance response for {wallet}: {res}")
    return res["result"]["value"] / 1e9

def rpc_get_usdc_balance(wallet):
    res = rpc_post({
        "jsonrpc": "2.0", "id": 1,
        "method": "getTokenAccountsByOwner",
        "params": [
            wallet,
            {"mint": USDC_MINT},
            {"encoding": "jsonParsed"},
        ],
    })
    if "result" not in res or "value" not in res.get("result", {}):
        raise Exception(f"Unexpected getTokenAccountsByOwner response for {wallet}: {res}")
    total = 0.0
    for acc in res["result"]["value"]:
        try:
            info = acc["account"]["data"]["parsed"]["info"]
            ta   = info["tokenAmount"]
            ui   = ta.get("uiAmount")
            if ui is not None:
                total += float(ui)
            else:
                amt = int(ta.get("amount", "0"))
                dec = int(ta.get("decimals", 0))
                total += amt / (10 ** dec) if dec else float(amt)
        except Exception:
            continue
    return total

# =========================
# NOTION
# =========================
def notion_headers():
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Content-Type": "application/json",
        "Notion-Version": NOTION_VERSION,
    }

def notion_req(url, body, method="POST"):
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers=notion_headers(),
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw) if raw else {}
            if isinstance(data, dict) and data.get("object") == "error":
                raise Exception(f"Notion error: {data}")
            return data
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise Exception(f"Notion HTTP {e.code}: {detail}")

def notion_get_prev_wallet_row(wallet, today):
    """
    Get the most recent row for this wallet where Date < today.
    Strictly excludes today to avoid same-day rows corrupting the delta.
    """
    body = {
        "filter": {
            "and": [
                {
                    "property": TITLE_PROP_PERWALLET,
                    "title": {"equals": wallet}
                },
                {
                    "property": "Date",
                    "date": {"before": today}
                }
            ]
        },
        "sorts": [{"property": "Date", "direction": "descending"}],
        "page_size": 1,
    }
    res = notion_req(
        f"https://api.notion.com/v1/databases/{NOTION_DB_PERWALLET}/query",
        body
    )
    results = res.get("results", [])
    return results[0] if results else None

def notion_get_prev_total_row(today):
    """
    Get the most recent daily total row where Date < today.
    Strictly excludes today to avoid same-day rows corrupting the delta.
    """
    body = {
        "filter": {
            "property": "Date",
            "date": {"before": today}
        },
        "sorts": [{"property": "Date", "direction": "descending"}],
        "page_size": 1,
    }
    res = notion_req(
        f"https://api.notion.com/v1/databases/{NOTION_DB_DAILYTOTAL}/query",
        body
    )
    results = res.get("results", [])
    return results[0] if results else None

def get_page_date(page):
    """Extract the date string from a Notion page's Date property."""
    try:
        return page["properties"]["Date"]["date"]["start"]
    except (KeyError, TypeError):
        return "unknown"

def get_prev_number(page, prop):
    if page is None:
        return 0.0
    try:
        val = page["properties"][prop]["number"]
        return float(val) if val is not None else 0.0
    except (KeyError, TypeError):
        return 0.0

def create_page(db_id, props):
    notion_req(
        "https://api.notion.com/v1/pages",
        {"parent": {"database_id": db_id}, "properties": props},
        method="POST",
    )

# =========================
# MAIN
# =========================
def main():
    if not NOTION_TOKEN:
        fail("NOTION_TOKEN missing")
    if not NOTION_DB_PERWALLET:
        fail("NOTION_DB_PERWALLET missing")
    if not NOTION_DB_DAILYTOTAL:
        fail("NOTION_DB_DAILYTOTAL missing")
    if not SOLANA_RPC_URL.startswith("https://"):
        fail(f"SOLANA_RPC_URL must start with https:// (got: {SOLANA_RPC_URL})")
    assert_pubkey("USDC_WALLET", USDC_WALLET)
    assert_pubkey("USDC_MINT",   USDC_MINT)

    wallets = parse_wallets(WALLETS_CSV)
    if not wallets:
        fail("No valid Solana pubkeys found in WALLETS_CSV.")

    today = datetime.now(timezone.utc).date().isoformat()

    log("----- PARSED WALLETS -----")
    for i, w in enumerate(wallets, 1):
        log(f"  {i:02d}: {w}")
    log(f"Total wallets: {len(wallets)}")
    log(f"USDC wallet:   {USDC_WALLET}")
    log(f"Date:          {today}")

    # --- Fetch USDC (one wallet only) ---
    log("----- FETCH USDC -----")
    usdc_total = r2(rpc_get_usdc_balance(USDC_WALLET))
    log(f"  {USDC_WALLET} -> USDC={usdc_total}")
    time.sleep(RPC_DELAY_USDC)

    # --- Fetch SOL (all wallets) ---
    per_rows = []
    total_sol = 0.0
    log("----- FETCH SOL -----")
    for i, w in enumerate(wallets, 1):
        sol  = r2(rpc_get_sol_balance(w))
        usdc = usdc_total if w == USDC_WALLET else 0.0
        per_rows.append((w, sol, usdc))
        total_sol += sol
        log(f"  {i:02d}/{len(wallets)} | {w} | SOL={sol} | USDC={usdc}")
        time.sleep(RPC_DELAY_SOL)

    total_sol = r2(total_sol)

    log("----- SORTED SOL BALANCES -----")
    for w, sol, _ in sorted(per_rows, key=lambda x: x[1], reverse=True):
        log(f"  {sol:>10.2f} | {w}")

    log("----- SUMMARY -----")
    log(f"  Wallet count:  {len(wallets)}")
    log(f"  Total SOL:     {total_sol}")
    log(f"  Total USDC:    {usdc_total}")

    # --- Write per-wallet rows ---
    log("----- WRITING PER WALLET ROWS -----")
    for w, sol, usdc in per_rows:
        prev_page  = notion_get_prev_wallet_row(w, today)
        prev_date  = get_page_date(prev_page) if prev_page else "none"
        prev_sol   = get_prev_number(prev_page, "End Balance")
        prev_usdc  = get_prev_number(prev_page, "USDC End Balance")
        sol_delta  = r2(sol  - prev_sol)  if prev_page else None
        usdc_delta = r2(usdc - prev_usdc) if prev_page else None

        log(f"  {w}")
        log(f"    prev date={prev_date}  prev_sol={prev_sol}  prev_usdc={prev_usdc}")
        log(f"    curr_sol={sol}  curr_usdc={usdc}")
        log(f"    sol_delta={sol_delta}  usdc_delta={usdc_delta}")

        create_page(NOTION_DB_PERWALLET, {
            TITLE_PROP_PERWALLET: {"title": [{"text": {"content": w}}]},
            "Date":             {"date": {"start": today}},
            "End Balance":      {"number": sol},
            "Delta":            {"number": sol_delta},
            "USDC End Balance": {"number": usdc},
            "USDC Delta":       {"number": usdc_delta},
        })

    # --- Write daily total row ---
    log("----- WRITING DAILY TOTAL ROW -----")
    prev_total_page  = notion_get_prev_total_row(today)
    prev_total_date  = get_page_date(prev_total_page) if prev_total_page else "none"
    prev_sol_total   = get_prev_number(prev_total_page, "End Balance")
    prev_usdc_total  = get_prev_number(prev_total_page, "USDC End Balance")
    sol_total_delta  = r2(total_sol  - prev_sol_total)  if prev_total_page else None
    usdc_total_delta = r2(usdc_total - prev_usdc_total) if prev_total_page else None

    log(f"  prev date={prev_total_date}  prev_sol={prev_sol_total}  prev_usdc={prev_usdc_total}")
    log(f"  curr_sol={total_sol}  curr_usdc={usdc_total}")
    log(f"  sol_delta={sol_total_delta}  usdc_delta={usdc_total_delta}")

    create_page(NOTION_DB_DAILYTOTAL, {
        "Name":             {"title": [{"text": {"content": f"{total_sol:.2f} SOL"}}]},
        "Date":             {"date": {"start": today}},
        "End Balance":      {"number": total_sol},
        "Delta":            {"number": sol_total_delta},
        "USDC End Balance": {"number": usdc_total},
        "USDC Delta":       {"number": usdc_total_delta},
    })

    log(f"  wrote daily total | SOL={total_sol} delta={sol_total_delta} | USDC={usdc_total} delta={usdc_total_delta}")
    log("Done.")

if __name__ == "__main__":
    main()
