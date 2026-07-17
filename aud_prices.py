#!/usr/bin/env python3
"""
AUD price helper (CoinGecko) — pure Python stdlib, no dependencies.

Shared by the SOL and ETH trackers and the backfill script.

  spot_aud(["solana", "ethereum"])   -> {"solana": 110.0, "ethereum": 2666.8}
  historical_aud("solana", "2026-02-08") -> 314.2   (or None if unavailable)

CoinGecko free ("demo") tier is used by default. Historical data is only
available for roughly the last 365 days on the free tier — older dates
return None. An optional demo API key (COINGECKO_API_KEY) raises the rate
limit and is sent via the x-cg-demo-api-key header.
"""
import os, json, time, random
import urllib.request, urllib.error
from datetime import datetime, timezone

CG_BASE     = os.environ.get("COINGECKO_BASE", "https://api.coingecko.com/api/v3").rstrip("/")
CG_KEY      = os.environ.get("COINGECKO_API_KEY", "").strip()
CG_TIMEOUT  = int(os.environ.get("CG_TIMEOUT", "30"))
CG_RETRIES  = int(os.environ.get("CG_RETRIES", "6"))
CG_UA       = os.environ.get("CG_USER_AGENT", "notion-tracker/1.0 (+https://github.com)").strip()

# Common CoinGecko coin ids.
COIN_IDS = {
    "SOL":  "solana",
    "ETH":  "ethereum",
    "USDC": "usd-coin",
    "USDT": "tether",
}

_hist_cache = {}   # (coin_id, date_iso) -> price | None


def _get(path, params):
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{CG_BASE}{path}?{qs}"
    headers = {"User-Agent": CG_UA, "Accept": "application/json"}
    if CG_KEY:
        headers["x-cg-demo-api-key"] = CG_KEY
    last_err = None
    for attempt in range(CG_RETRIES):
        try:
            req = urllib.request.Request(url, headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=CG_TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8", errors="replace") or "{}")
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code}"
            # 429 = rate limited; back off and retry. 5xx too.
            if e.code in (429, 500, 502, 503, 504):
                d = min(2 ** attempt + random.uniform(0, 0.8), 60.0)
                print(f"  [coingecko] {last_err}, retry in {d:.1f}s", flush=True)
                time.sleep(d)
            else:
                raise Exception(f"CoinGecko {last_err}: {e.read().decode()[:200]}")
        except Exception as ex:
            last_err = str(ex)
            d = min(2 ** attempt + random.uniform(0, 0.8), 60.0)
            print(f"  [coingecko] {last_err}, retry in {d:.1f}s", flush=True)
            time.sleep(d)
    raise Exception(f"CoinGecko request failed after {CG_RETRIES} tries: {last_err}")


def spot_aud(coin_ids):
    """Current AUD price for each coin id. Returns {coin_id: float}."""
    ids = ",".join(dict.fromkeys(coin_ids))
    data = _get("/simple/price", {"ids": ids, "vs_currencies": "aud"})
    return {cid: (data.get(cid, {}) or {}).get("aud") for cid in coin_ids}


def historical_aud(coin_id, date_iso):
    """
    AUD price of coin_id on a given date (YYYY-MM-DD).
    Returns a float, or None if CoinGecko has no data for that date
    (e.g. older than the free-tier 365-day window). Cached per (coin, date).
    """
    key = (coin_id, date_iso)
    if key in _hist_cache:
        return _hist_cache[key]
    y, m, d = date_iso.split("-")
    cg_date = f"{d}-{m}-{y}"   # CoinGecko wants DD-MM-YYYY
    data = _get(f"/coins/{coin_id}/history", {"date": cg_date, "localization": "false"})
    price = (data.get("market_data", {}) or {}).get("current_price", {}).get("aud")
    _hist_cache[key] = price
    return price


def daily_series_aud(coin_id, days=365):
    """
    Bulk daily AUD price history for a coin in ONE call.
    Returns {date_iso: price} for roughly the last `days` days (free-tier
    cap is 365). Much cheaper than one historical_aud() call per date.
    """
    data = _get(f"/coins/{coin_id}/market_chart",
                {"vs_currency": "aud", "days": str(days), "interval": "daily"})
    out = {}
    for ts, price in data.get("prices", []):
        d = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).date().isoformat()
        out[d] = price
    return out


def price_on(series, date_iso):
    """Price for a date from a daily series; falls back to the nearest
    earlier date if that exact day is missing. None if nothing on/before."""
    if date_iso in series:
        return series[date_iso]
    earlier = [d for d in series if d <= date_iso]
    return series[max(earlier)] if earlier else None


if __name__ == "__main__":
    print("spot:", spot_aud(["solana", "ethereum", "tether", "usd-coin"]))
    print("hist SOL 2026-02-08:", historical_aud("solana", "2026-02-08"))
    s = daily_series_aud("solana", 365)
    print("series points:", len(s), "| SOL 2026-02-08:", price_on(s, "2026-02-08"))
