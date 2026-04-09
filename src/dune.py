"""
Dune Analytics integration — free historical batch scanning.

Dune indexes the full Solana transaction history and exposes it via SQL
(DuneSQL / Trino dialect). This complements Helius:

  Helius  → real-time monitoring, rich parsed data, limited free credits
  Dune    → historical SQL over all Solana data, free tier adequate for daily sweeps

Key tables used:
  dex_solana.trades      — DEX swaps (Raydium, Jupiter, Orca, Pump.fun …)
  tokens_solana.transfers — raw token transfers

Free tier notes:
  - Web UI queries: unlimited (manual execution)
  - API executions: ~1,500 credits/month on free (each query = 10–100 credits)
  - Results cache: re-fetching latest results from a scheduled query is free
  - Best practice: schedule queries on the Dune website to run daily, then
    just fetch the cached results via API (costs 0 credits).

Setup:
  1. Create an account at dune.com
  2. Create the two queries below (or fork from the provided templates)
  3. Set your API key: DUNE_API_KEY=... in .env
  4. Optionally note the query IDs: DUNE_SELL_QUERY_ID / DUNE_COPURCHASE_QUERY_ID
"""
import asyncio
import json
import time
from typing import Optional, Any

import httpx

from src.config import DB_PATH
from src.db import get_db, save_candidate


# ── DuneSQL query templates ──────────────────────────────────────────────────
# These are parameterized queries. Paste them into dune.com/queries/new.
# Parameters: {{wallets_csv}} (comma-separated wallet list), {{window_seconds}}

SELL_CLUSTER_QUERY = """
-- Coordinated Sell Cluster Detection
-- Finds wallet pairs from your tracked list that sold the same token
-- within {{window_seconds}} seconds of each other.
-- Paste your wallet CSV into the wallets_csv parameter.
WITH wallet_sells AS (
    SELECT
        trader_a                    AS wallet,
        token_sold_mint_address     AS mint,
        block_time,
        tx_id                       AS signature
    FROM dex_solana.trades
    WHERE trader_a IN (
        SELECT trim(w) FROM UNNEST(split('{{wallets_csv}}', ',')) AS t(w)
    )
    -- Exclude stablecoin and SOL sells (those are buy-side of a swap, not token sells)
    AND token_sold_mint_address NOT IN (
        'So11111111111111111111111111111111111111112',   -- wSOL
        'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v', -- USDC
        'Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB',  -- USDT
        '7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs'  -- ETH (Wormhole)
    )
    AND block_time >= now() - interval '30' day
)
SELECT
    a.wallet                                            AS addr1,
    b.wallet                                            AS addr2,
    count(DISTINCT a.mint)                              AS co_sell_count,
    array_agg(DISTINCT a.mint)                          AS token_mints,
    avg(abs(date_diff('second', a.block_time, b.block_time))) AS avg_time_diff_seconds,
    min(abs(date_diff('second', a.block_time, b.block_time))) AS min_time_diff_seconds
FROM wallet_sells a
JOIN wallet_sells b
    ON  a.mint    = b.mint
    AND a.wallet  < b.wallet
    AND abs(date_diff('second', a.block_time, b.block_time)) <= {{window_seconds}}
GROUP BY a.wallet, b.wallet
HAVING count(DISTINCT a.mint) >= 1
ORDER BY avg_time_diff_seconds ASC, co_sell_count DESC
"""

CO_PURCHASE_QUERY = """
-- Co-Purchase Pattern Detection
-- Finds wallet pairs that have bought the same tokens at any point in time.
-- No time window — consistency across the full history is the signal.
WITH wallet_buys AS (
    SELECT
        trader_a                    AS wallet,
        token_bought_mint_address   AS mint,
        block_time
    FROM dex_solana.trades
    WHERE trader_a IN (
        SELECT trim(w) FROM UNNEST(split('{{wallets_csv}}', ',')) AS t(w)
    )
    AND token_bought_mint_address NOT IN (
        'So11111111111111111111111111111111111111112',
        'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v',
        'Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB',
        '7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs'
    )
    AND block_time >= now() - interval '90' day
)
SELECT
    a.wallet                        AS addr1,
    b.wallet                        AS addr2,
    count(DISTINCT a.mint)          AS shared_tokens,
    array_agg(DISTINCT a.mint)      AS token_mints
FROM wallet_buys a
JOIN wallet_buys b
    ON  a.mint   = b.mint
    AND a.wallet < b.wallet
GROUP BY a.wallet, b.wallet
HAVING count(DISTINCT a.mint) >= {{min_shared}}
ORDER BY shared_tokens DESC
"""


# ── Dune API client ──────────────────────────────────────────────────────────

DUNE_API_BASE = "https://api.dune.com/api/v1"

_DUNE_RESULT_CACHE_HOURS = 20  # Don't re-execute if results are fresher than this


class DuneClient:
    """
    Minimal Dune Analytics API client.

    Usage pattern (credit-efficient):
      1. Create + schedule your queries on dune.com (runs daily for free)
      2. Use fetch_latest_results(query_id) to pull cached results — 0 credits
      3. Use execute_query(query_id, params) only when you need fresh data

    Free tier: ~1,500 execution credits/month. Fetching scheduled results
    is free and doesn't count against your credit limit.
    """

    def __init__(self, api_key: str):
        self._key = api_key
        self._headers = {
            "X-DUNE-API-KEY": api_key,
            "Content-Type": "application/json",
        }
        self._client = httpx.AsyncClient(timeout=60.0)

    async def close(self):
        await self._client.aclose()

    # ── Core API methods ─────────────────────────────────────────────────────

    async def execute_query(
        self,
        query_id: int,
        params: dict[str, Any] | None = None,
    ) -> str:
        """
        Trigger a query execution. Returns execution_id.
        Costs Dune execution credits — use sparingly on free tier.
        """
        body: dict[str, Any] = {}
        if params:
            body["query_parameters"] = {
                k: str(v) for k, v in params.items()
            }
        resp = await self._client.post(
            f"{DUNE_API_BASE}/query/{query_id}/execute",
            headers=self._headers,
            json=body,
        )
        resp.raise_for_status()
        return resp.json()["execution_id"]

    async def wait_for_results(
        self,
        execution_id: str,
        poll_interval: float = 3.0,
        timeout: float = 120.0,
    ) -> list[dict]:
        """Poll until execution completes, return rows."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            resp = await self._client.get(
                f"{DUNE_API_BASE}/execution/{execution_id}/results",
                headers=self._headers,
            )
            resp.raise_for_status()
            data = resp.json()
            state = data.get("state", "")
            if state == "QUERY_STATE_COMPLETED":
                return data.get("result", {}).get("rows", [])
            if state in ("QUERY_STATE_FAILED", "QUERY_STATE_CANCELLED"):
                raise RuntimeError(f"Dune query {state}: {data.get('error')}")
            await asyncio.sleep(poll_interval)
        raise TimeoutError(f"Dune query timed out after {timeout}s")

    async def fetch_latest_results(self, query_id: int) -> list[dict]:
        """
        Fetch the most recent cached results for a query — 0 execution credits.
        Only works if the query has been run at least once (manually or scheduled).
        """
        resp = await self._client.get(
            f"{DUNE_API_BASE}/query/{query_id}/results",
            headers=self._headers,
        )
        resp.raise_for_status()
        return resp.json().get("result", {}).get("rows", [])

    # ── High-level scan methods ──────────────────────────────────────────────

    async def run_sell_cluster_scan(
        self,
        query_id: int,
        wallets: list[str],
        window_seconds: int = 10,
        use_cache: bool = True,
    ) -> list[dict]:
        """
        Run (or fetch cached results of) the sell-cluster query.

        Returns rows: {addr1, addr2, co_sell_count, token_mints,
                       avg_time_diff_seconds, min_time_diff_seconds}

        Set use_cache=False to force a fresh execution (costs credits).
        """
        if use_cache:
            rows = await self.fetch_latest_results(query_id)
        else:
            wallets_csv = ",".join(wallets)
            eid = await self.execute_query(query_id, {
                "wallets_csv": wallets_csv,
                "window_seconds": window_seconds,
            })
            rows = await self.wait_for_results(eid)
        return rows

    async def run_co_purchase_scan(
        self,
        query_id: int,
        wallets: list[str],
        min_shared: int = 3,
        use_cache: bool = True,
    ) -> list[dict]:
        """
        Run (or fetch cached results of) the co-purchase query.

        Returns rows: {addr1, addr2, shared_tokens, token_mints}
        """
        if use_cache:
            rows = await self.fetch_latest_results(query_id)
        else:
            wallets_csv = ",".join(wallets)
            eid = await self.execute_query(query_id, {
                "wallets_csv": wallets_csv,
                "min_shared": min_shared,
            })
            rows = await self.wait_for_results(eid)
        return rows


# ── Result importers — save Dune findings into local DB ─────────────────────

def import_sell_clusters(rows: list[dict], registry, path: str = DB_PATH) -> int:
    """
    Save Dune sell-cluster results into the candidates table.
    Returns number of new candidates saved.
    """
    from src.matcher import _fee_confidence  # avoid circular

    count = 0
    for row in rows:
        addr1 = row.get("addr1", "")
        addr2 = row.get("addr2", "")
        co_sells = int(row.get("co_sell_count", 0))
        avg_dt = float(row.get("avg_time_diff_seconds") or 10)
        min_dt = float(row.get("min_time_diff_seconds") or 10)

        # Mirror coordinated_sell_scan confidence formula
        conf = 0.70
        if min_dt <= 2:
            conf += 0.20
        elif min_dt <= 5:
            conf += 0.12
        elif min_dt <= 10:
            conf += 0.05
        extra = max(co_sells - 1, 0)
        conf = min(conf + extra * 0.08, 0.97)

        mints = row.get("token_mints") or []
        if isinstance(mints, str):
            mints = mints.split(",")

        evidence = {
            "source": "dune",
            "co_sell_count": co_sells,
            "avg_time_diff_seconds": avg_dt,
            "min_time_diff_seconds": min_dt,
            "token_mints": mints[:10],
        }
        save_candidate(addr1, addr2, "coordinated_sell", conf, evidence, path)
        save_candidate(addr2, addr1, "coordinated_sell", conf, evidence, path)
        count += 2

    return count


def import_co_purchases(rows: list[dict], registry, path: str = DB_PATH) -> int:
    """
    Save Dune co-purchase results into the candidates table.
    Returns number of new candidates saved.
    """
    count = 0
    for row in rows:
        addr1 = row.get("addr1", "")
        addr2 = row.get("addr2", "")
        shared = int(row.get("shared_tokens", 0))

        conf = 0.40 + min((shared - 3) * 0.08, 0.48)
        conf = min(round(conf, 2), 0.88)

        mints = row.get("token_mints") or []
        if isinstance(mints, str):
            mints = mints.split(",")

        evidence = {
            "source": "dune",
            "shared_tokens": shared,
            "token_mints": mints[:10],
        }
        save_candidate(addr1, addr2, "co_purchase_pattern", conf, evidence, path)
        save_candidate(addr2, addr1, "co_purchase_pattern", conf, evidence, path)
        count += 2

    return count


def print_query_setup_guide():
    """Print instructions for setting up Dune queries."""
    print("""
╔══════════════════════════════════════════════════════╗
║           Dune Analytics Setup Guide                 ║
╚══════════════════════════════════════════════════════╝

1. Go to  https://dune.com/queries/new

2. Create the SELL CLUSTER query:
   - Paste the SQL from SELL_CLUSTER_QUERY in src/dune.py
   - Add parameters:  wallets_csv (text), window_seconds (number, default: 10)
   - Save and note the query ID from the URL

3. Create the CO-PURCHASE query:
   - Paste the SQL from CO_PURCHASE_QUERY in src/dune.py
   - Add parameters:  wallets_csv (text), min_shared (number, default: 3)
   - Save and note the query ID

4. Schedule both queries to run daily (Settings → Schedule → Daily)
   Scheduled runs are FREE and don't cost execution credits.

5. Add to your .env:
   DUNE_API_KEY=your_api_key_here
   DUNE_SELL_QUERY_ID=123456
   DUNE_COPURCHASE_QUERY_ID=789012

6. Run daily sweep:
   python discover.py dune-scan

Free tier credit usage:
  - Fetching scheduled results = 0 credits
  - Manual re-execution = ~10-50 credits per query
  - 1,500 free credits/month = plenty for daily use
""")
