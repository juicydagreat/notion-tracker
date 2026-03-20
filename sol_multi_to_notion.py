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
# ENV / CONFIG
# =========================
NOTION_TOKEN = os.environ["NOTION_TOKEN"].strip()
NOTION_DB_PERWALLET = os.environ["NOTION_DB_PERWALLET"].strip()
NOTION_DB_DAILYTOTAL = os.environ["NOTION_DB_DAILYTOTAL"].strip()
WALLETS_CSV = os.environ["WALLETS_CSV"]

SOLANA_RPC_URL = os.environ.get(
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

NOTION_VERSION = os.environ.get("NOTION_VERSION", "2022-06-28").strip()

TITLE_PROP_PERWALLET = os.environ.get("TITLE_PROP_PERWALLET", "Wallet").strip()
TOTAL_TITLE_PROP = os.environ.get("TOTAL_TITLE_PROP", "Name").strip()
DATE_PROP = os.environ.get("DATE_PROP", "Date").strip()

PROP_SOL_END   = os.environ.get("PROP_SOL_END",   "End Balance").strip()
PROP_SOL_DELTA = os.environ.get("PROP_SOL_DELTA", "Delta").strip()
PROP_USDC_END   = os.environ.get("PROP_USDC_END",   "USDC End Balance").strip()
PROP_USDC_DELTA = os.environ.get("PROP_USDC_DELTA", "USDC Delta").strip()

RPC_TIMEOUT     = int(os.environ.get("RPC_TIMEOUT",     "30"))
RPC_RETRIES     = int(os.environ.get("RPC_RETRIES",     "8"))
RPC_DELAY_SOL   = float(os.environ.get("RPC_DELAY_SOL", "0.35"))
RPC_DELAY_USDC  = float(os.environ.get("RPC_DELAY_USDC","0.50"))
RPC_BACKOFF_CAP = float(os.environ.get("RPC_BACKOFF_CAP","30"))

NOTION_TIMEOUT     = int(os.environ.get("NOTION_TIMEOUT",     "30"))
NOTION_RETRIES     = int(os.environ.get("NOTION_RETRIES",     "8"))
NOTION_BACKOFF_CAP = float(os.environ.get("NOTION_BACKOFF_CAP","30"))

PUBKEY_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")


# =========================
# HELPERS
# =========================
def fail(msg: str) -> None:
    print(f"ERROR: {msg}", flush=True)
    sys.exit(1)


def log(msg: str) -> None:
    print(msg, flush=True)


def r2(x):
    return None if x is None else round(float(x), 2)


def parse_wallets(raw: str) -> list[str]:
    found = PUBKEY_RE.findall(raw or "")
    out, seen = [], set()
    for w in found:
        if w not in seen:
            seen.add(w)
            out.append(w)
    return out


def assert_pubkey(name: str, value: str) -> None:
    if not value or not PUBKEY_RE.fullmatch(value):
        fail(f"{name} is not a valid Solana pubkey: '{value}'")


def backoff_sleep(attempt: int, cap: float) -> None:
    delay = min((2 ** attempt) + random.uniform(0.0, 0.8), cap)
    log(f"  Retrying after {delay:.2f}s...")
    time.sleep(delay)


# =========================
# HTTP JSON (shared retry layer)
# =========================
def http_json(
    url: str,
    method: str,
    headers: dict,
    body: dict | None,
    timeout: int,
    retries: int,
    backoff_cap: float,
    label: str,
) -> dict:
    data = None if body is None else json.dumps(body).encode("utf-8")
    last_err = None

    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url, data=data, headers=headers, method=method.upper()
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                parsed = json.loads(raw) if raw else {}
                if isinstance(parsed, dict) and parsed.get("error") is not None:
                    raise Exception(f"{label} API error: {parsed['error']}")
                return parsed

        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            last_err = f"{label} HTTP {e.code} {e.reason}: {detail}"

            if e.code == 429:
                log(f"  {label} rate limit: {last_err}")
                if label == "RPC" and "max usage reached" in detail:
                    raise Exception(f"RPC quota exhausted: {last_err}")
                backoff_sleep(attempt + 1, backoff_cap)
                continue

            if e.code in (408, 425, 500, 502, 503, 504):
                log(f"  {label} transient error: {last_err}")
                backoff_sleep(attempt + 1, backoff_cap)
                continue

            raise Exception(last_err)

        except Exception as e:
            last_err = str(e)
            log(f"  {label} request error: {last_err}")
            backoff_sleep(attempt + 1, backoff_cap)

    raise Exception(f"{label} failed after {retries} retries. Last: {last_err}")


# =========================
# SOLANA RPC
# =========================
def rpc_post(payload: dict) -> dict:
    return http_json(
        url=SOLANA_RPC_URL,
        method="POST",
        headers={"Content-Type": "application/json"},
        body=payload,
        timeout=RPC_TIMEOUT,
        retries=RPC_RETRIES,
        backoff_cap=RPC_BACKOFF_CAP,
        label="RPC",
    )


def rpc_get_sol_balance(wallet: str) -> float:
    res = rpc_post({
        "jsonrpc": "2.0", "id": 1,
        "method": "getBalance",
        "params": [wallet],
    })
    if "result" not in res or "value" not in res["result"]:
        raise Exception(f"RPC missing result for getBalance({wallet}): {res}")
    return res["result"]["value"] / 1e9


def rpc_get_usdc_balance(wallet: str) -> float:
    res = rpc_post({
        "jsonrpc": "2.0", "id": 1,
        "method": "getTokenAccountsByOwner",
        "params": [wallet, {"mint": USDC_MINT}, {"encoding": "jsonParsed"}],
    })
    if "result" not in res or "value" not in res["result"]:
        raise Exception(
            f"RPC missing result for getTokenAccountsByOwner({wallet}): {res}"
        )

    total = 0.0
    for acc in res["result"]["value"]:
        try:
            info = acc["account"]["data"]["parsed"]["info"]
            ta = info["tokenAmount"]
            ui_amt = ta.get("uiAmount")
            if ui_amt is not None:
                total += float(ui_amt)
            else:
                amt = int(ta.get("amount", "0"))
                dec = int(ta.get("decimals", 0))
                total += amt / (10 ** dec) if dec else float(amt)
        except Exception as e:
            # Log parse failures — a silent zero here would corrupt the USDC total
            log(f"  WARNING: skipping token account (parse error): {e} | raw: {acc}")
            continue

    return total


# =========================
# NOTION
# =========================
def notion_headers() -> dict:
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Content-Type": "application/json",
        "Notion-Version": NOTION_VERSION,
    }


def notion_req(url: str, body: dict | None = None, method: str = "POST") -> dict:
    return http_json(
        url=url,
        method=method,
        headers=notion_headers(),
        body=body,
        timeout=NOTION_TIMEOUT,
        retries=NOTION_RETRIES,
        backoff_cap=NOTION_BACKOFF_CAP,
        label="Notion",
    )


def notion_query(db_id: str, body: dict) -> dict:
    return notion_req(
        f"https://api.notion.com/v1/databases/{db_id}/query", body, method="POST"
    )


def notion_create_page(db_id: str, props: dict) -> dict:
    return notion_req(
        "https://api.notion.com/v1/pages",
        {"parent": {"database_id": db_id}, "properties": props},
        method="POST",
    )


def notion_update_page(page_id: str, props: dict) -> dict:
    return notion_req(
        f"https://api.notion.com/v1/pages/{page_id}",
        {"properties": props},
        method="PATCH",
    )


def get_page_id(page: dict) -> str:
    return page.get("id")


def get_number(page: dict | None, prop: str):
    if not page:
        return None
    try:
        p = page["properties"][prop]
        if p.get("type") == "number":
            return p.get("number")
    except Exception:
        return None
    return None


def find_today_per_wallet_page(db_id: str, wallet: str, date_iso: str) -> dict | None:
    res = notion_query(db_id, {
        "filter": {"and": [
            {"property": TITLE_PROP_PERWALLET, "title": {"equals": wallet}},
            {"property": DATE_PROP, "date": {"equals": date_iso}},
        ]},
        "page_size": 1,
    })
    results = res.get("results", [])
    return results[0] if results else None


def find_prev_per_wallet_page(db_id: str, wallet: str, date_iso: str) -> dict | None:
    res = notion_query(db_id, {
        "filter": {"and": [
            {"property": TITLE_PROP_PERWALLET, "title": {"equals": wallet}},
            {"property": DATE_PROP, "date": {"before": date_iso}},
        ]},
        "sorts": [{"property": DATE_PROP, "direction": "descending"}],
        "page_size": 1,
    })
    results = res.get("results", [])
    return results[0] if results else None


def find_today_total_page(db_id: str, date_iso: str) -> dict | None:
    res = notion_query(db_id, {
        "filter": {"property": DATE_PROP, "date": {"equals": date_iso}},
        "page_size": 1,
    })
    results = res.get("results", [])
    return results[0] if results else None


def find_prev_total_page(db_id: str, date_iso: str) -> dict | None:
    res = notion_query(db_id, {
        "filter": {"property": DATE_PROP, "date": {"before": date_iso}},
        "sorts": [{"property": DATE_PROP, "direction": "descending"}],
        "page_size": 1,
    })
    results = res.get("results", [])
    return results[0] if results else None


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
        fail(f"SOLANA_RPC_URL must be https:// (got {SOLANA_RPC_URL})")

    assert_pubkey("USDC_WALLET", USDC_WALLET)
    assert_pubkey("USDC_MINT", USDC_MINT)

    wallets = parse_wallets(WALLETS_CSV)
    if not wallets:
        fail("No valid Solana pubkeys found in WALLETS_CSV.")

    for w in wallets:
        assert_pubkey("Wallet", w)

    # Fail fast if USDC_WALLET is absent from the wallet list.
    # If missing, its per-wallet Notion row would be written with usdc=0.0
    # and its USDC delta would always be None — silently wrong.
    if USDC_WALLET not in wallets:
        fail(
            f"USDC_WALLET ({USDC_WALLET}) is not present in WALLETS_CSV. "
            f"Add it so its per-wallet Notion row is written correctly."
        )

    today = datetime.now(timezone.utc).date().isoformat()

    log("----- PARSED WALLETS -----")
    for i, w in enumerate(wallets, 1):
        log(f"  {i:02d}: {w}")
    log(f"  Total wallets : {len(wallets)}")
    log(f"  USDC wallet   : {USDC_WALLET}")
    log(f"  Date          : {today}")

    log("----- FETCH USDC -----")
    usdc_total = r2(rpc_get_usdc_balance(USDC_WALLET))
    log(f"  {USDC_WALLET} -> USDC={usdc_total}")
    time.sleep(RPC_DELAY_USDC)

    per_rows = []
    total_sol = 0.0

    log("----- FETCH SOL -----")
    for i, w in enumerate(wallets, 1):
        sol = r2(rpc_get_sol_balance(w))
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
    log(f"  Wallet count : {len(wallets)}")
    log(f"  Total SOL    : {total_sol}")
    log(f"  Total USDC   : {usdc_total}")

    prev_total_page  = find_prev_total_page(NOTION_DB_DAILYTOTAL, today)
    prev_total_sol   = get_number(prev_total_page, PROP_SOL_END)
    prev_total_usdc  = get_number(prev_total_page, PROP_USDC_END)

    total_sol_delta  = None if prev_total_sol  is None else r2(total_sol  - float(prev_total_sol))
    total_usdc_delta = None if prev_total_usdc is None else r2(usdc_total - float(prev_total_usdc))

    log("----- WRITING PER-WALLET ROWS -----")
    for w, sol, usdc in per_rows:
        prev_page  = find_prev_per_wallet_page(NOTION_DB_PERWALLET, w, today)
        prev_sol   = get_number(prev_page, PROP_SOL_END)
        prev_usdc  = get_number(prev_page, PROP_USDC_END)

        sol_delta  = None if prev_sol  is None else r2(sol  - float(prev_sol))
        usdc_delta = None if prev_usdc is None else r2(usdc - float(prev_usdc))

        props = {
            TITLE_PROP_PERWALLET: {"title": [{"text": {"content": w}}]},
            DATE_PROP:            {"date":   {"start": today}},
            PROP_SOL_END:         {"number": sol},
            PROP_SOL_DELTA:       {"number": sol_delta},
            PROP_USDC_END:        {"number": usdc},
            PROP_USDC_DELTA:      {"number": usdc_delta},
        }

        today_page = find_today_per_wallet_page(NOTION_DB_PERWALLET, w, today)
        if today_page:
            notion_update_page(get_page_id(today_page), props)
        else:
            notion_create_page(NOTION_DB_PERWALLET, props)

        log(f"  wrote {w} | SOL={sol} Δ={sol_delta} | USDC={usdc} Δ={usdc_delta}")

    log("----- WRITING DAILY TOTAL ROW -----")
    total_props = {
        TOTAL_TITLE_PROP: {"title": [{"text": {"content": f"{total_sol:.2f} SOL"}}]},
        DATE_PROP:        {"date":   {"start": today}},
        PROP_SOL_END:     {"number": total_sol},
        PROP_SOL_DELTA:   {"number": total_sol_delta},
        PROP_USDC_END:    {"number": usdc_total},
        PROP_USDC_DELTA:  {"number": total_usdc_delta},
    }

    today_total_page = find_today_total_page(NOTION_DB_DAILYTOTAL, today)
    if today_total_page:
        notion_update_page(get_page_id(today_total_page), total_props)
        log("  updated today's total row")
    else:
        notion_create_page(NOTION_DB_DAILYTOTAL, total_props)
        log("  created today's total row")

    log("Done.")


if __name__ == "__main__":
    main()
