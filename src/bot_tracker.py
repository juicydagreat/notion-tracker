"""
Copy Bot Lead Analysis

When a copy trading bot automatically mirrors a wallet, it buys the same tokens
N seconds AFTER the wallet it copies.  By working backwards from the bot's
buy history we can identify which wallet(s) it is copying.

Method
------
1. Fetch bot's recent buy history (token + block_time per signature).
2. For each bot buy, query the local token_purchases DB for wallets that bought
   the same token 0-max_lag seconds BEFORE the bot.
3. Wallets that consistently precede the bot across many tokens = copy sources.

This is powerful when combined with the co-purchase and sell-cluster detectors:
a wallet that (a) consistently leads the bot, (b) co-buys the same tokens as
other tracked wallets, and (c) sells simultaneously is almost certainly the
same person operating multiple wallets.

Seeding token data
------------------
`seed_token_buyers()` fetches all transactions for a specific token mint from
the free public Solana RPC, extracts who bought/sold, and stores them in the
local token_purchases DB.  This is the fastest way to populate data for a set
of known training tokens without waiting for the daemon to run.

Real-time integration
---------------------
The WalletMonitor daemon can watch a bot wallet the same way it watches normal
wallets.  When the bot buys, _find_leads_in_db() runs instantly (zero API
credits) against the already-populated token_purchases table.

Dune complement
---------------
Use `discover.py dune-bot-leads <bot>` for full historical analysis via the
BOT_LEADS_QUERY SQL template (see src/dune.py).  Scheduled daily at no credit
cost; fetching cached results is free.
"""
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional, Callable

from src.config import DB_PATH
from src.db import get_db, save_bot_lead, save_candidate, save_token_purchase
from src.helius import HeliusClient, extract_token_actions
from src.wallets import WalletRegistry


# Stablecoins / wrapped SOL — not meaningful as trade signals
_EXCLUDED_MINTS: frozenset[str] = frozenset({
    "So11111111111111111111111111111111111111112",    # wSOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
    "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs",  # ETH (Wormhole)
    "11111111111111111111111111111111",               # System Program
})


@dataclass
class BotLeadResult:
    wallet: str
    tokens_led: int           # distinct tokens where this wallet preceded the bot
    avg_lag_seconds: float    # average seconds between their buy and the bot's buy
    min_lag_seconds: float    # tightest observed lag
    token_mints: list[str] = field(default_factory=list)
    known_label: Optional[str] = None
    confidence: float = 0.0


@dataclass
class TokenBotBuy:
    """One token buy by the bot — used as a reference point."""
    token_mint: str
    bot_block_time: int
    bot_signature: str
    bot_slot: int


# ── Internal helpers ──────────────────────────────────────────────────────────

def _fee_payer_from_tx(tx_data: dict, fallback: str) -> str:
    accounts = (
        tx_data.get("transaction", {})
        .get("message", {})
        .get("accountKeys", [])
    )
    for acc in accounts:
        if isinstance(acc, dict):
            if acc.get("signer") and acc.get("writable"):
                return acc.get("pubkey", fallback)
        elif isinstance(acc, str):
            return acc
    return fallback


async def _get_bot_buys(
    bot_wallet: str,
    client: HeliusClient,
    lookback: int = 100,
) -> list[TokenBotBuy]:
    """
    Fetch the bot's recent token buy history via free RPC.
    Returns one TokenBotBuy per (signature, token_mint) pair where the bot bought.
    """
    sigs = await client.get_signatures(bot_wallet, limit=lookback)
    buys: list[TokenBotBuy] = []

    for sig in sigs:
        if sig.err or not sig.block_time:
            continue
        tx = await client.get_transaction(sig.signature)
        if not tx:
            continue

        fee_payer = _fee_payer_from_tx(tx, bot_wallet)
        actions = extract_token_actions(tx, fee_payer)

        for mint, direction in actions.items():
            if direction == "buy" and mint not in _EXCLUDED_MINTS:
                buys.append(TokenBotBuy(
                    token_mint=mint,
                    bot_block_time=sig.block_time,
                    bot_signature=sig.signature,
                    bot_slot=sig.slot,
                ))

    return buys


def _find_leads_in_db(
    token_mint: str,
    bot_block_time: int,
    bot_wallet: str,
    max_lag: int = 60,
    path: str = DB_PATH,
) -> list[dict]:
    """
    Query token_purchases for wallets that bought token_mint in the window
    [bot_block_time - max_lag, bot_block_time - 1] — strictly before the bot.

    Returns [{address, block_time, fee, signature, lag_seconds}]
    sorted by lag_seconds ascending (tightest first).
    """
    lo = bot_block_time - max_lag
    hi = bot_block_time - 1
    with get_db(path) as db:
        rows = db.execute(
            """
            SELECT
                address,
                block_time,
                fee,
                signature,
                (? - block_time) AS lag_seconds
            FROM token_purchases
            WHERE token_mint  = ?
              AND direction   = 'buy'
              AND block_time  BETWEEN ? AND ?
              AND address    != ?
            ORDER BY lag_seconds ASC
            """,
            (bot_block_time, token_mint, lo, hi, bot_wallet),
        ).fetchall()
    return [dict(r) for r in rows]


def _confidence(tokens_led: int, avg_lag: float) -> float:
    """
    Confidence that a wallet is the bot's copy source.

    tokens_led=1  → 0.50 base (could be coincidence)
    tokens_led=2  → 0.60
    tokens_led=3  → 0.70
    tokens_led=4+ → up to 0.85
    tight avg lag → +0.10 (≤5s) / +0.07 (≤15s) / +0.04 (≤30s)
    max           → 0.95
    """
    conf = 0.50 + min((tokens_led - 1) * 0.10, 0.35)
    if avg_lag <= 5:
        conf += 0.10
    elif avg_lag <= 15:
        conf += 0.07
    elif avg_lag <= 30:
        conf += 0.04
    return min(round(conf, 2), 0.95)


# ── Public API ────────────────────────────────────────────────────────────────

async def seed_token_buyers(
    token_mint: str,
    client: HeliusClient,
    around_time: Optional[int] = None,
    window_seconds: int = 300,
    max_fetch: int = 500,
    on_progress: Optional[Callable[[int, int], None]] = None,
    path: str = DB_PATH,
) -> int:
    """
    Fetch all transactions for a token mint via free RPC and store buyers/sellers
    in the token_purchases table.

    This seeds the local DB so that `investigate` and `find_bot_leads` have data
    to work with immediately, without needing to run the daemon first.

    Args:
        token_mint:     The mint address to scan (e.g. a Pump.fun token).
        client:         HeliusClient (uses free RPC — 0 Helius credits).
        around_time:    Unix timestamp to center the scan on.  If given, only
                        transactions within ±window_seconds of this time are
                        saved.  If None, all fetched transactions are saved.
        window_seconds: Half-width of the time window when around_time is set.
        max_fetch:      Maximum total signatures to fetch (pagination cap).
        on_progress:    Optional callback(fetched, saved) called each page.
        path:           SQLite DB path.

    Returns:
        Number of token purchase/sell records saved.

    How it works:
        `getSignaturesForAddress(mint)` returns every transaction that touched
        the token's mint account — every buy, sell, transfer, and mint.
        We fetch these newest-first, stopping once we've gone far enough back
        in time, then resolve each tx to extract who bought or sold.

    Free RPC cost:
        1 call per signature page (up to 1000 sigs) + 1 call per tx.
        For a 5-minute window on an active Pump.fun token: typically 50-200 txs.
    """
    # Phase 1: collect signatures in the time window
    collected: list[tuple[str, int, int]] = []  # (sig, slot, block_time)
    before: Optional[str] = None
    fetched_total = 0

    lo_time = (around_time - window_seconds) if around_time else 0
    hi_time = (around_time + window_seconds) if around_time else int(time.time()) + 9999

    while fetched_total < max_fetch:
        page = await client.get_signatures(token_mint, limit=min(200, max_fetch - fetched_total), before=before)
        if not page:
            break

        for sig in page:
            if sig.err:
                continue
            bt = sig.block_time or 0
            # If we've gone before the window, stop paginating
            if around_time and bt < lo_time:
                fetched_total = max_fetch  # signal outer loop to stop
                break
            if lo_time <= bt <= hi_time:
                collected.append((sig.signature, sig.slot, bt))

        fetched_total += len(page)
        before = page[-1].signature

        if len(page) < 200:
            break  # end of history

    if not collected:
        return 0

    # Phase 2: fetch each tx and extract token actions
    saved = 0
    for i, (sig, slot, block_time) in enumerate(collected):
        tx = await client.get_transaction(sig)
        if not tx:
            continue

        # Determine fee payer
        accounts = (
            tx.get("transaction", {})
            .get("message", {})
            .get("accountKeys", [])
        )
        meta = tx.get("meta", {}) or {}
        fee = meta.get("fee")
        fee_payer: Optional[str] = None
        for acc in accounts:
            if isinstance(acc, dict):
                if acc.get("signer") and acc.get("writable"):
                    fee_payer = acc.get("pubkey")
                    break
            elif isinstance(acc, str) and fee_payer is None:
                fee_payer = acc

        if not fee_payer:
            continue

        actions = extract_token_actions(tx, fee_payer)
        direction = actions.get(token_mint)
        if direction in ("buy", "sell"):
            save_token_purchase(
                address=fee_payer,
                token_mint=token_mint,
                slot=slot,
                block_time=block_time,
                fee=fee,
                signature=sig,
                direction=direction,
                path=path,
            )
            saved += 1

        if on_progress:
            on_progress(i + 1, saved)

    return saved


async def find_bot_leads(
    bot_wallet: str,
    client: HeliusClient,
    registry: WalletRegistry,
    lookback: int = 100,
    max_lag: int = 60,
    path: str = DB_PATH,
) -> list[BotLeadResult]:
    """
    Analyze a copy bot to find the wallets it is copying.

    Workflow:
      1. Fetch bot's last `lookback` transactions via free RPC.
      2. For each token buy, query local token_purchases DB for lead wallets.
      3. Persist each lead event to bot_leads table.
      4. Rank results by tokens_led DESC, avg_lag ASC.

    Best results when the daemon has been running to populate token_purchases.
    For fresh installs without daemon data, use `dune-bot-leads` instead.

    Returns a list of BotLeadResult, highest-confidence first.
    """
    bot_buys = await _get_bot_buys(bot_wallet, client, lookback)

    # lead_wallet → {token_mint: best_lag_record}
    lead_data: dict[str, dict[str, dict]] = defaultdict(dict)

    for buy in bot_buys:
        leads = _find_leads_in_db(buy.token_mint, buy.bot_block_time, bot_wallet, max_lag, path)
        for lead in leads:
            addr = lead["address"]
            lag = lead["lag_seconds"]
            existing = lead_data[addr].get(buy.token_mint)
            # Keep the record with the smallest lag per (wallet, token)
            if existing is None or lag < existing["lag_seconds"]:
                lead_data[addr][buy.token_mint] = {
                    "lead_block_time": lead["block_time"],
                    "bot_block_time": buy.bot_block_time,
                    "lag_seconds": lag,
                    "lead_sig": lead.get("signature"),
                    "bot_sig": buy.bot_signature,
                }
                save_bot_lead(
                    bot_wallet=bot_wallet,
                    lead_wallet=addr,
                    token_mint=buy.token_mint,
                    lead_block_time=lead["block_time"],
                    bot_block_time=buy.bot_block_time,
                    lag_seconds=lag,
                    lead_sig=lead.get("signature"),
                    bot_sig=buy.bot_signature,
                    path=path,
                )

    results: list[BotLeadResult] = []
    for wallet, token_map in lead_data.items():
        lags = [v["lag_seconds"] for v in token_map.values()]
        tokens_led = len(token_map)
        avg_lag = sum(lags) / len(lags)
        min_lag = min(lags)
        conf = _confidence(tokens_led, avg_lag)

        w = registry.get(wallet)
        results.append(BotLeadResult(
            wallet=wallet,
            tokens_led=tokens_led,
            avg_lag_seconds=avg_lag,
            min_lag_seconds=min_lag,
            token_mints=list(token_map.keys()),
            known_label=w.label if w else None,
            confidence=conf,
        ))

        save_candidate(wallet, bot_wallet, "bot_lead", conf, {
            "source": "bot_lead",
            "bot_wallet": bot_wallet,
            "tokens_led": tokens_led,
            "avg_lag_seconds": round(avg_lag, 1),
            "min_lag_seconds": min_lag,
            "token_mints": list(token_map.keys())[:10],
        }, path)

    return sorted(results, key=lambda r: (-r.tokens_led, r.avg_lag_seconds))


async def investigate_tokens(
    token_mints: list[str],
    client: HeliusClient,
    registry: WalletRegistry,
    bot_wallet: Optional[str] = None,
    max_lag: int = 60,
    path: str = DB_PATH,
) -> dict:
    """
    Given a list of token mints (e.g., tokens a specific trader bought),
    find which wallets bought those tokens — and optionally which of those
    bought them BEFORE a known copy bot.

    This is the entry point for the "training data" use case:
      - User provides known tokens + the bot that copies the target trader
      - We find who bought those tokens before the bot → those are the alts

    Returns:
      {
        "bot_buys":        {token_mint: TokenBotBuy},
        "buyers_per_token": {token_mint: [lead_records]},
        "intersection":    [wallet_address],   # bought ALL provided tokens
        "ranked":          [BotLeadResult],
      }
    """
    result: dict = {
        "bot_buys": {},
        "buyers_per_token": {},
        "intersection": [],
        "ranked": [],
    }

    # Step 1: If bot given, find its buy time for each token
    bot_buy_times: dict[str, TokenBotBuy] = {}
    if bot_wallet:
        bot_buys = await _get_bot_buys(bot_wallet, client, lookback=200)
        for buy in bot_buys:
            if buy.token_mint in token_mints:
                # Keep earliest buy per token
                existing = bot_buy_times.get(buy.token_mint)
                if existing is None or buy.bot_block_time < existing.bot_block_time:
                    bot_buy_times[buy.token_mint] = buy
        result["bot_buys"] = bot_buy_times

    # Step 2: For each token find buyers (before bot if bot given, else all).
    # Auto-seed from chain if the local DB has no data for a token.
    lead_data: dict[str, dict[str, dict]] = defaultdict(dict)

    for mint in token_mints:
        # Check if we already have local data for this mint
        with get_db(path) as db:
            local_count = db.execute(
                "SELECT COUNT(*) FROM token_purchases WHERE token_mint = ?", (mint,)
            ).fetchone()[0]

        if local_count == 0:
            # Auto-seed: fetch buyers from chain (free RPC)
            ref_time = bot_buy_times[mint].bot_block_time if mint in bot_buy_times else None
            result.setdefault("seeded", {})[mint] = await seed_token_buyers(
                token_mint=mint,
                client=client,
                around_time=ref_time,
                window_seconds=max(max_lag * 3, 300),  # generous window
                path=path,
            )

        if bot_wallet and mint in bot_buy_times:
            bot_buy = bot_buy_times[mint]
            leads = _find_leads_in_db(mint, bot_buy.bot_block_time, bot_wallet, max_lag, path)
        else:
            # No bot reference — return all buyers of this token from DB
            with get_db(path) as db:
                rows = db.execute(
                    """SELECT address, block_time, fee, signature, 0 AS lag_seconds
                       FROM token_purchases
                       WHERE token_mint = ? AND direction = 'buy'
                       ORDER BY block_time""",
                    (mint,),
                ).fetchall()
            leads = [dict(r) for r in rows]

        result["buyers_per_token"][mint] = leads

        for lead in leads:
            addr = lead["address"]
            if addr == bot_wallet:
                continue
            existing = lead_data[addr].get(mint)
            lag = lead.get("lag_seconds", 0)
            if existing is None or lag < existing.get("lag_seconds", lag):
                lead_data[addr][mint] = {
                    "block_time": lead["block_time"],
                    "lag_seconds": lag,
                    "signature": lead.get("signature"),
                }

    # Step 3: Intersection — wallets that appear across ALL provided tokens
    result["intersection"] = [
        w for w, token_map in lead_data.items()
        if len(token_map) == len(token_mints)
    ]

    # Step 4: Rank by coverage + lag tightness
    ranked: list[BotLeadResult] = []
    for wallet, token_map in lead_data.items():
        lags = [v["lag_seconds"] for v in token_map.values() if v.get("lag_seconds") is not None]
        tokens_led = len(token_map)
        avg_lag = sum(lags) / len(lags) if lags else max_lag
        min_lag = min(lags) if lags else max_lag
        conf = _confidence(tokens_led, avg_lag)

        w = registry.get(wallet)
        ranked.append(BotLeadResult(
            wallet=wallet,
            tokens_led=tokens_led,
            avg_lag_seconds=avg_lag,
            min_lag_seconds=min_lag,
            token_mints=list(token_map.keys()),
            known_label=w.label if w else None,
            confidence=conf,
        ))

        if bot_wallet:
            save_candidate(wallet, bot_wallet, "bot_lead", conf, {
                "source": "investigate",
                "bot_wallet": bot_wallet,
                "tokens_investigated": token_mints,
                "tokens_matched": tokens_led,
                "avg_lag_seconds": round(avg_lag, 1),
            }, path)

    result["ranked"] = sorted(ranked, key=lambda r: (-r.tokens_led, r.avg_lag_seconds))
    return result
