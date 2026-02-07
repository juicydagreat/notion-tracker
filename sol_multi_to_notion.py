import os
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
NOTION_TOKEN = os.environ["NOTION_TOKEN"]
NOTION_DB_PERWALLET = os.environ["NOTION_DB_PERWALLET"]
NOTION_DB_DAILYTOTAL = os.environ["NOTION_DB_DAILYTOTAL"]
WALLETS_CSV = os.environ["WALLETS_CSV"]
SOLANA_RPC_URL = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")

# USDC mint (Solana mainnet)
USDC_MINT = "EPjFWdd5AufqSSqeM2q4Y9Jv6R3hHc3zZkZz8pJ9oG"


# =========================
# HELPERS
# =========================
def r2(x):
    return None if x is None else round(float(x), 2)


def parse_wallets(raw: str) -> list[str]:
    """
    Robust wallet extraction:
    - Instead of splitting by commas/newlines (prone to hidden chars),
      we extract valid base58 pubkeys of length 32-44.
    - Dedupes while preserving order.
    """
    if not raw:
        return []

    # Solana pubkey base58 alphabet excludes 0,O,I,l
    pattern = r"[1-9A-HJ-NP-Za-km-z]{32,44}"
    candidates = re.findall(pattern, raw)

    out = []
    seen = set()
    for w in candidates:
        if w not in seen:
            out.append(w)
            seen.add(w)
    return out


def rpc_post(payload: dict, *, retries: int = 8, timeout: int = 30) -> dict:
    last_err = None

    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(
                SOLANA_RPC_URL,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )

            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")

            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                raise Exception(f"RPC returned non-JSON response: {raw[:400]}")

            if isinstance(data, dict) and data.get("error") is not None:
                raise Exception(f"Solana RPC error: {data['error']}")

            if not isinstance(data, dict):
                raise Exception(f"RPC returned unexpected payload type: {type(data)}")

            return data

        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")
            except Exception:
                body = ""
            last_err = f"HTTPError {e.code}: {e.reason}. Body: {body[:400]}"

            if e.code not in (429, 500, 502, 503, 504):
                raise Exception(f"RPC hard failure: {last_err}")

        except Exception as e:
            last_err = str(e)

        # exponential backoff + jitter
        sleep_s = min(2 ** attempt, 30) + random.uniform(0.0, 0.8)
        time.sleep(sleep_s)

    raise Exception(f"RPC failed after retries. Last error: {last_err}")


def rpc_get_sol_balance(wallet: str) -> float:
    payload = {"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [wallet]}
    res = rpc_post(payload)
    if "result" not in res or res["result"] is None or "value" not in res["result"]:
        raise Exception(f"RPC missing result for getBalance({wallet}). Response: {res}")
    return res["result"]["value"] / 1e9


def rpc_get_usdc_balance(wallet: str) -> float:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTokenAccountsByOwner",
        "params": [
            wallet,
            {"mint": USDC_MINT},
            {"encoding": "jsonParsed"},
        ],
    }
    res = rpc_post(payload)
    if "result" not in res or res["result"] is None or "value" not in res["result"]:
        raise Exception(f"RPC missing result for getTokenAccountsByOwner({wallet}). Response: {res}")

    total = 0.0
    for acc in res["result"]["value"]:
        try:
            amt = acc["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"]
            if amt is not None:
                total += float(amt)
        except Exception:
            continue

    return total


def notion_headers():
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28",
    }


def notion_number(v):
    return None if v is None else {"number": v}


def notion_date(d):
    return {"date": {"start": d}}


def notion_create_page(db, props):
    body = {"parent": {"database_id": db}, "properties": props}

    req = urllib.request.Request(
        "https://api.notion.com/v1/pages",
        data=json.dumps(body).encode("utf-8"),
        headers=notion_headers(),
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8", errors="replace")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise Exception(f"Notion returned non-JSON: {raw[:400]}")

    if isinstance(data, dict) and data.get("object") == "error":
        raise Exception(f"Notion error: {data}")

    return data


# =========================
# MAIN
# =========================
def main():
    wallets = parse_wallets(WALLETS_CSV)
    if not wallets:
        raise Exception(
            "No valid Solana pubkeys found in WALLETS_CSV. "
            "Make sure the secret contains Solana addresses."
        )

    per_wallet = []
    total_sol = 0.0
    total_usdc = 0.0

    for w in wallets:
        try:
            sol_raw = rpc_get_sol_balance(w)
        except Exception as e:
            raise Exception(f"SOL balance failed for wallet {w}: {e}")

        try:
            usdc_raw = rpc_get_usdc_balance(w)
        except Exception as e:
            raise Exception(f"USDC balance failed for wallet {w}: {e}")

        sol = r2(sol_raw)
        usdc = r2(usdc_raw)

        per_wallet.append((w, sol, usdc))
        total_sol += sol
        total_usdc += usdc

    total_sol = r2(total_sol)
    total_usdc = r2(total_usdc)

    today = datetime.now(timezone.utc).date().isoformat()

    # PER WALLET
    for w, sol, usdc in per_wallet:
        props = {
            "Wallet": {"title": [{"text": {"content": w}}]},
            "Date": notion_date(today),
            "End Balance": notion_number(sol),
            "USDC End Balance": notion_number(usdc),
        }
        notion_create_page(NOTION_DB_PERWALLET, props)

    # DAILY TOTAL
    total_props = {
        "Name": {"title": [{"text": {"content": f"{total_sol:.2f} SOL"}}]},
        "Date": notion_date(today),
        "End Balance": notion_number(total_sol),
        "USDC End Balance": notion_number(total_usdc),
    }
    notion_create_page(NOTION_DB_DAILYTOTAL, total_props)


if __name__ == "__main__":
    main()
