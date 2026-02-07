import os, sys, json, time, urllib.request, urllib.error
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Optional, Tuple, Dict, Any, List

RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com").strip()
NOTION_TOKEN = os.getenv("NOTION_TOKEN", "").strip()
DB_PER = os.getenv("NOTION_DB_PERWALLET", "").strip()
DB_TOTAL = os.getenv("NOTION_DB_DAILYTOTAL", "").strip()
WALLETS = [w.strip() for w in (os.getenv("WALLETS_CSV") or "").split(",") if w.strip()]

TITLE_PROP_PERWALLET = os.getenv("TITLE_PROP_PERWALLET", "Name").strip()
TOTAL_TITLE_PROP = os.getenv("TOTAL_TITLE_PROP", "Name").strip()
WALLET_ADDR_PROP = os.getenv("WALLET_ADDR_PROP", "Wallet Address").strip()

# Canonical Solana USDC mint
USDC_MINT = os.getenv("USDC_MINT", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v").strip()

# Token-2022 program (optional support)
TOKEN_2022_PROGRAM_ID = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

AEST = ZoneInfo("Australia/Brisbane")


def fail(msg: str):
    print(f"ERROR: {msg}")
    sys.exit(1)


def notion_req(url: str, body: dict | None = None, method: str = "POST") -> dict:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "Authorization": f"Bearer {NOTION_TOKEN}",
            "Content-Type": "application/json",
            "Notion-Version": "2022-06-28",
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode() or "{}"
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        detail = e.read().decode()
        fail(f"Notion {e.code} {e.reason}: {detail}")


def rpc_post(payload: dict, max_retries: int = 6) -> dict:
    if not RPC_URL.startswith("https://"):
        fail(f"SOLANA_RPC_URL must be HTTPS; got '{RPC_URL}'")

    data = json.dumps(payload).encode()
    req = urllib.request.Request(RPC_URL, data=data, headers={"Content-Type": "application/json"})

    backoff = 1.0
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code} {e.reason}: {e.read().decode(errors='replace')}"
            if e.code == 429:
                time.sleep(backoff)
                backoff = min(backoff * 2.0, 16.0)
                continue
            fail(f"RPC error: {last_err}")
        except Exception as e:
            last_err = str(e)
            time.sleep(backoff)
            backoff = min(backoff * 2.0, 16.0)

    fail(f"RPC failed after retries: {last_err}")


def rpc_get_sol_balance(wallet: str) -> float:
    payload = {"jsonrpc":"2.0","id":1,"method":"getBalance","params":[wallet, {"commitment":"finalized"}]}
    out = rpc_post(payload)
    try:
        lamports = out["result"]["value"]
    except Exception:
        fail(f"Unexpected getBalance response for {wallet}: {out}")
    return lamports / 1_000_000_000


def _sum_from_accounts_value(value: list, mint: str) -> float:
    total = 0.0
    for item in value:
        try:
            info = item["account"]["data"]["parsed"]["info"]
            if info.get("mint") != mint:
                continue
            amt = info["tokenAmount"]
            s = amt.get("uiAmountString")
            if s is not None:
                total += float(s)
            else:
                ui = amt.get("uiAmount")
                total += float(ui or 0.0)
        except Exception:
            continue
    return float(total)


def rpc_get_usdc_balance(wallet: str) -> float:
    """
    Correct way to read USDC:
    1) Classic SPL: filter by mint (this should catch real USDC)
    2) Token-2022 (optional): query by programId, then filter by mint client-side
    """
    # 1) Mint-filtered query (works for canonical USDC)
    payload1 = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTokenAccountsByOwner",
        "params": [
            wallet,
            {"mint": USDC_MINT},
            {"encoding": "jsonParsed"}
        ]
    }
    out1 = rpc_post(payload1)
    value1 = ((out1.get("result") or {}).get("value") or [])
    total = _sum_from_accounts_value(value1, USDC_MINT)

    # 2) Token-2022 fallback (usually 0 for USDC, but harmless and future-proof)
    payload2 = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTokenAccountsByOwner",
        "params": [
            wallet,
            {"programId": TOKEN_2022_PROGRAM_ID},
            {"encoding": "jsonParsed"}
        ]
    }
    out2 = rpc_post(payload2)
    value2 = ((out2.get("result") or {}).get("value") or [])
    total += _sum_from_accounts_value(value2, USDC_MINT)

    return float(total)


def _num(props: dict, name: str) -> Optional[float]:
    v = props.get(name, {}).get("number")
    return None if v is None else float(v)


def per_wallet_latest_row(db_id: str, wallet_addr: str) -> Optional[dict]:
    body = {
        "filter": {"property": WALLET_ADDR_PROP, "rich_text": {"equals": wallet_addr}},
        "sorts": [{"property": "Date", "direction": "descending"}],
        "page_size": 1
    }
    res = notion_req(f"https://api.notion.com/v1/databases/{db_id}/query", body)
    results = res.get("results", [])
    return results[0] if results else None


def latest_total_row(db_id: str) -> Optional[dict]:
    body = {"sorts": [{"property": "Date", "direction": "descending"}], "page_size": 1}
    res = notion_req(f"https://api.notion.com/v1/databases/{db_id}/query", body)
    results = res.get("results", [])
    return results[0] if results else None


def create_per_wallet_row(
    db_id: str, date_iso: str, wallet_addr: str,
    sol_end: float, sol_delta: Optional[float],
    usdc_end: float, usdc_delta: Optional[float],
):
    props: Dict[str, Any] = {
        "Date": {"date": {"start": date_iso}},
        TITLE_PROP_PERWALLET: {"title": [{"text": {"content": f"{sol_end:.2f} SOL"}}]},
        WALLET_ADDR_PROP: {"rich_text": [{"text": {"content": wallet_addr}}]},
        "End Balance": {"number": round(sol_end, 2)},
        "USDC End Balance": {"number": round(usdc_end, 2)},
    }
    if sol_delta is not None:
        props["Delta"] = {"number": round(sol_delta, 2)}
    if usdc_delta is not None:
        props["USDC Delta"] = {"number": round(usdc_delta, 2)}

    body = {"parent": {"database_id": db_id}, "properties": props}
    notion_req("https://api.notion.com/v1/pages", body)


def create_daily_total_row(db_id: str, date_iso: str, sol_total_end: float, sol_total_delta: Optional[float]):
    props: Dict[str, Any] = {
        TOTAL_TITLE_PROP: {"title": [{"text": {"content": f"{sol_total_end:.2f} SOL"}}]},
        "Date": {"date": {"start": date_iso}},
        "End Balance": {"number": round(sol_total_end, 2)},
    }
    if sol_total_delta is not None:
        props["Delta"] = {"number": round(sol_total_delta, 2)}

    body = {"parent": {"database_id": db_id}, "properties": props}
    notion_req("https://api.notion.com/v1/pages", body)


def main():
    if not NOTION_TOKEN: fail("NOTION_TOKEN missing")
    if not DB_PER: fail("NOTION_DB_PERWALLET missing")
    if not DB_TOTAL: fail("NOTION_DB_DAILYTOTAL missing")
    if not WALLETS: fail("WALLETS_CSV missing or empty")

    today_aest = datetime.now(timezone.utc).astimezone(AEST).date().isoformat()

    per_rows = []
    for w in WALLETS:
        sol_end = rpc_get_sol_balance(w)
        usdc_end = rpc_get_usdc_balance(w)

        prev = per_wallet_latest_row(DB_PER, w)
        if prev is None:
            sol_delta = None
            usdc_delta = None
        else:
            props = prev.get("properties", {})
            prev_sol = _num(props, "End Balance")
            prev_usdc = _num(props, "USDC End Balance")
            sol_delta = None if prev_sol is None else (sol_end - prev_sol)
            usdc_delta = None if prev_usdc is None else (usdc_end - prev_usdc)

        per_rows.append((w, sol_end, sol_delta, usdc_end, usdc_delta))

    for w, sol_end, sol_delta, usdc_end, usdc_delta in per_rows:
        create_per_wallet_row(DB_PER, today_aest, w, sol_end, sol_delta, usdc_end, usdc_delta)

    sol_total_end = sum(x[1] for x in per_rows)
    prev_total = latest_total_row(DB_TOTAL)
    if prev_total is None:
        sol_total_delta = None
    else:
        props = prev_total.get("properties", {})
        prev_sol_total = _num(props, "End Balance")
        sol_total_delta = None if prev_sol_total is None else (sol_total_end - prev_sol_total)

    create_daily_total_row(DB_TOTAL, today_aest, sol_total_end, sol_total_delta)

    print(f"{today_aest} | wallets={len(per_rows)} | USDC_MINT={USDC_MINT}")
    for w, sol_end, sol_delta, usdc_end, usdc_delta in per_rows:
        sd = "None" if sol_delta is None else f"{sol_delta:+.2f}"
        ud = "None" if usdc_delta is None else f"{usdc_delta:+.2f}"
        print(f"  {w[:8]}… SOL={sol_end:.2f} Δ={sd} | USDC={usdc_end:.2f} Δ={ud}")


if __name__ == "__main__":
    main()
