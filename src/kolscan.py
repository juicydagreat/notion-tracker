"""
KolScan leaderboard checker.

KolScan (kolscan.io) lists top Solana traders.  A wallet that appears on the
leaderboard is considered the "main" identity wallet — it gets UPPERCASE naming.

Uses best-effort scraping; returns None gracefully if the site is unreachable.
"""
import re
from typing import Optional

import httpx


KOLSCAN_BASE = "https://kolscan.io"
KOLSCAN_LEADERBOARD = KOLSCAN_BASE + "/leaderboard"
KOLSCAN_WALLET_URL = KOLSCAN_BASE + "/account/{address}"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/json,*/*",
}

# Simple in-process cache so we don't re-fetch per wallet in a batch
_leaderboard_cache: Optional[set[str]] = None
_name_cache: dict[str, str] = {}


async def _fetch_leaderboard_addresses(client: httpx.AsyncClient) -> set[str]:
    """Scrape leaderboard page(s) and return all wallet addresses found."""
    addresses: set[str] = set()
    # Try JSON API first (kolscan exposes one at /api/leaderboard on some builds)
    for api_url in [
        KOLSCAN_BASE + "/api/leaderboard",
        KOLSCAN_BASE + "/api/v1/leaderboard",
        KOLSCAN_BASE + "/api/accounts/leaderboard",
    ]:
        try:
            r = await client.get(api_url, headers=_HEADERS)
            if r.status_code == 200:
                data = r.json()
                addrs = _extract_addresses_from_json(data)
                if addrs:
                    addresses.update(addrs)
                    return addresses
        except Exception:
            pass

    # Fallback: scrape the HTML leaderboard page
    try:
        r = await client.get(KOLSCAN_LEADERBOARD, headers=_HEADERS)
        if r.status_code == 200:
            # Solana addresses: base58, 32–44 chars
            found = re.findall(r'[1-9A-HJ-NP-Za-km-z]{32,44}', r.text)
            addresses.update(found)
    except Exception:
        pass

    return addresses


def _extract_addresses_from_json(data) -> list[str]:
    """Recursively find address-shaped strings in arbitrary JSON."""
    results = []
    if isinstance(data, dict):
        for key in ("address", "wallet", "walletAddress", "pubkey", "account"):
            val = data.get(key)
            if isinstance(val, str) and 32 <= len(val) <= 44:
                results.append(val)
        for v in data.values():
            results.extend(_extract_addresses_from_json(v))
    elif isinstance(data, list):
        for item in data:
            results.extend(_extract_addresses_from_json(item))
    return results


async def is_on_leaderboard(address: str) -> bool:
    """
    Return True if this address appears on the KolScan leaderboard.
    Caches leaderboard for the process lifetime.
    """
    global _leaderboard_cache
    if _leaderboard_cache is None:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            _leaderboard_cache = await _fetch_leaderboard_addresses(client)
    return address in _leaderboard_cache


async def get_leaderboard_name(address: str) -> Optional[str]:
    """
    Try to fetch the display name assigned to an address on KolScan.
    Returns the name string or None.
    """
    if address in _name_cache:
        return _name_cache[address]

    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            url = KOLSCAN_WALLET_URL.format(address=address)
            r = await client.get(url, headers=_HEADERS)
            if r.status_code != 200:
                return None
            # Try JSON response
            try:
                data = r.json()
                name = (
                    data.get("name")
                    or data.get("username")
                    or (data.get("data") or {}).get("name")
                )
                if name:
                    _name_cache[address] = name
                    return name
            except Exception:
                pass
            # Fallback: parse title / og:title from HTML
            m = re.search(r'<title[^>]*>([^<]+)</title>', r.text, re.IGNORECASE)
            if m:
                title = m.group(1).strip()
                # Strip common suffixes like "| KolScan"
                title = re.sub(r'\s*[\|–-].*$', '', title).strip()
                if title and address[:8] not in title:
                    _name_cache[address] = title
                    return title
    except Exception:
        pass
    return None


async def identify_main_wallet(addresses: list[str]) -> Optional[str]:
    """
    Given a list of addresses, return the one that appears on KolScan.
    If multiple appear, return the first found.
    Returns None if none are on the leaderboard.
    """
    for addr in addresses:
        if await is_on_leaderboard(addr):
            return addr
    return None


def wallet_page_url(address: str) -> str:
    return KOLSCAN_WALLET_URL.format(address=address)
