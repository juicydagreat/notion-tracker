"""
GMGN.ai wallet PnL fetcher.

Used to determine the "main" wallet in a cluster when the wallet is not on
KolScan leaderboard — highest realized PnL = most likely primary wallet.

API is unofficial / no auth required for basic wallet stats.
"""
import asyncio
from typing import Optional

import httpx


GMGN_BASE = "https://gmgn.ai"
GMGN_WALLET_URL = GMGN_BASE + "/sol/address/{address}"

# Known API endpoints (unofficial, may change)
_STAT_ENDPOINTS = [
    GMGN_BASE + "/defi/quotation/v1/wallet_info/sol/{address}",
    GMGN_BASE + "/api/v1/wallet_stat/sol/{address}/7d",
]

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html,*/*",
    "Referer": "https://gmgn.ai/",
}


async def get_wallet_pnl(address: str, client: Optional[httpx.AsyncClient] = None) -> Optional[float]:
    """
    Fetch realized PnL (USD) for a Solana wallet from GMGN.

    Returns the realized PnL as a float, or None if unavailable.
    Tries multiple known API endpoint patterns.
    """
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=15.0, follow_redirects=True)

    try:
        for tmpl in _STAT_ENDPOINTS:
            url = tmpl.format(address=address)
            try:
                resp = await client.get(url, headers=_HEADERS)
                if resp.status_code != 200:
                    continue
                data = resp.json()
                pnl = _extract_pnl(data)
                if pnl is not None:
                    return pnl
            except Exception:
                continue
        return None
    finally:
        if own_client:
            await client.aclose()


def _extract_pnl(data: dict) -> Optional[float]:
    """Try to pull realized PnL from various GMGN response shapes."""
    if not isinstance(data, dict):
        return None

    # Shape 1: {"data": {"realized_profit": ...}}
    inner = data.get("data") or data
    if isinstance(inner, dict):
        for key in ("realized_profit", "realizedProfit", "realized_pnl", "pnl"):
            val = inner.get(key)
            if val is not None:
                try:
                    return float(val)
                except (TypeError, ValueError):
                    pass

        # Shape 2: nested under wallet / info
        for sub_key in ("wallet", "info", "stat"):
            sub = inner.get(sub_key)
            if isinstance(sub, dict):
                for key in ("realized_profit", "realizedProfit", "realized_pnl", "pnl"):
                    val = sub.get(key)
                    if val is not None:
                        try:
                            return float(val)
                        except (TypeError, ValueError):
                            pass
    return None


async def get_pnl_batch(addresses: list[str]) -> dict[str, Optional[float]]:
    """
    Fetch realized PnL for multiple addresses concurrently.
    Returns {address: pnl_usd or None}.
    """
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        tasks = {addr: get_wallet_pnl(addr, client) for addr in addresses}
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    return {
        addr: (r if not isinstance(r, Exception) else None)
        for addr, r in zip(tasks.keys(), results)
    }


def wallet_page_url(address: str) -> str:
    """Return the GMGN wallet page URL for manual inspection."""
    return GMGN_WALLET_URL.format(address=address)
