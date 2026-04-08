"""
Twitter / X identity search for Solana wallet addresses.

Searches public Twitter/X for mentions of a wallet address to find the
trader's identity, provide name suggestions, and add attribution confidence.

No API key required — uses public search via nitter mirrors (privacy-respecting
Twitter frontends) with fallback to direct search URL generation.
"""
import re
from dataclasses import dataclass, field
from typing import Optional

import httpx


# Public nitter instances (try in order, fall back gracefully)
_NITTER_HOSTS = [
    "https://nitter.privacydev.net",
    "https://nitter.poast.org",
    "https://nitter.1d4.us",
    "https://nitter.kavin.rocks",
]

_TWITTER_SEARCH_URL = "https://twitter.com/search?q={query}&src=typed_query"
_X_SEARCH_URL = "https://x.com/search?q={query}&src=typed_query"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,*/*",
}


@dataclass
class TwitterHit:
    address: str
    query_url: str                       # Link user can open to verify
    suggested_name: Optional[str] = None # Extracted from tweet content
    mentions: int = 0                    # Number of results found
    snippets: list[str] = field(default_factory=list)  # Raw text excerpts


async def search_wallet(address: str) -> TwitterHit:
    """
    Search Twitter/X for mentions of a wallet address.

    Always returns a TwitterHit — if scraping fails the hit will have
    mentions=0 but a valid query_url the user can open manually.
    """
    query_url = _X_SEARCH_URL.format(query=address)
    hit = TwitterHit(address=address, query_url=query_url)

    html = await _fetch_nitter(address)
    if html:
        hits = _parse_nitter_results(html)
        hit.mentions = len(hits)
        hit.snippets = hits[:5]
        hit.suggested_name = _guess_name(hits, address)

    return hit


async def _fetch_nitter(address: str) -> Optional[str]:
    """Try each nitter instance and return the first successful HTML."""
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
        for host in _NITTER_HOSTS:
            url = f"{host}/search?q={address}&f=tweets"
            try:
                r = await client.get(url, headers=_HEADERS)
                if r.status_code == 200 and len(r.text) > 500:
                    return r.text
            except Exception:
                continue
    return None


def _parse_nitter_results(html: str) -> list[str]:
    """Extract tweet text snippets from nitter HTML."""
    snippets = []
    # nitter wraps tweet content in <div class="tweet-content ...">
    matches = re.findall(
        r'class="tweet-content[^"]*"[^>]*>(.*?)</div>',
        html,
        re.DOTALL | re.IGNORECASE,
    )
    for raw in matches:
        text = re.sub(r'<[^>]+>', ' ', raw)   # strip tags
        text = re.sub(r'\s+', ' ', text).strip()
        if text:
            snippets.append(text)
    return snippets


def _guess_name(snippets: list[str], address: str) -> Optional[str]:
    """
    Heuristic: look for patterns like "@handle is address" or
    "address = @handle" in tweet snippets to extract a Twitter handle.
    """
    short = address[:8]
    handle_pattern = re.compile(r'@([A-Za-z0-9_]{2,30})')
    candidate_handles: dict[str, int] = {}

    for snippet in snippets:
        # Only consider snippets that actually mention the address / short prefix
        if short not in snippet and address not in snippet:
            continue
        handles = handle_pattern.findall(snippet)
        for h in handles:
            candidate_handles[h] = candidate_handles.get(h, 0) + 1

    if not candidate_handles:
        return None
    # Return the most-mentioned handle
    best = max(candidate_handles, key=lambda h: candidate_handles[h])
    return f"@{best}"


async def search_wallets_batch(addresses: list[str]) -> dict[str, TwitterHit]:
    """Search multiple addresses; returns {address: TwitterHit}."""
    import asyncio
    results = await asyncio.gather(
        *[search_wallet(addr) for addr in addresses],
        return_exceptions=True,
    )
    out = {}
    for addr, res in zip(addresses, results):
        if isinstance(res, Exception):
            out[addr] = TwitterHit(
                address=addr,
                query_url=_X_SEARCH_URL.format(query=addr),
            )
        else:
            out[addr] = res
    return out


def search_url(address: str) -> str:
    """Return an X/Twitter search URL for manual verification."""
    return _X_SEARCH_URL.format(query=address)
