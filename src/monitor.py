"""
Continuous wallet monitor — polls tracked wallets for new transactions and
automatically triggers:

  1. Same-block fee matching  — same slot + same fee (strongest signal)
  2. Temporal token matching  — same token within ±N minutes, different block
                                (catches rebuys and staggered entries)

Token co-buyer detection (in-block):
  Same block + same fee + shared DEX pool/token account → confidence 0.85-0.97

Temporal token detection (cross-block):
  Same token mint, different block, within 5-minute window.
  Confidence layers:
    0.25 base | +0.35 same fee | +0.20 within 60s | +0.10/shared token pattern

Credit budget:
  - Signature polling → free public RPC (0 Helius credits)
  - getTransaction for fee + mints → free public RPC (0 Helius credits)
  - getBlock → Helius only, ~10 credits, triggered by unique fees, cached forever
"""
import asyncio
import heapq
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Awaitable, Optional

from src.config import DEFAULT_FEE_LAMPORTS, MAX_CREDITS_PER_RUN
from src.helius import HeliusClient, BlockTx, extract_token_actions
from src.db import (
    cache_signatures, cache_tx, get_cached_tx,
    get_last_signature, is_slot_scanned, mark_slot_scanned,
    save_candidate, save_token_purchase,
)
from src.wallets import WalletRegistry
from src.matcher import MatchResult, _fee_confidence, co_purchase_pattern_scan, coordinated_sell_scan


# ── Solana system / infrastructure program addresses ─────────────────────────
# Transactions nearly always include these — not useful as "shared token" signal
BORING_ACCOUNTS: frozenset[str] = frozenset({
    "11111111111111111111111111111111",                      # System Program
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",         # Token Program
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",         # Token-2022
    "ComputeBudget111111111111111111111111111111",           # Compute Budget
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJe1bRS",        # Associated Token
    "SysvarRent111111111111111111111111111111111",
    "SysvarC1ock11111111111111111111111111111111",
    "SysvarEpochSchedu1e111111111111111111111111",
    "Vote111111111111111111111111111111111111111h",
    "BPFLoaderUpgradeab1e11111111111111111111111",
    "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s",         # Metaplex
})

# Known DEX / DeFi programs (buying via these = token purchase)
DEX_PROGRAMS: frozenset[str] = frozenset({
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",        # Raydium V4
    "5quBtoiQqxF9Jv6KYKctB59NT3gtJD2Y65kdnB1Uev3h",        # Raydium AMM v3
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK",        # Raydium CLMM
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",        # Jupiter v6
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3sFt2R7",         # Orca Whirlpool
    "9W959DqEETiGZocYWCQPaJ6sBmUzgfxXfqGeTEdp3aQP",        # Orca v2
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ35MKDatoJtZ8",        # Pump.fun
    "TSWAPaqyCSx2KABk68Shruf4rp7CxcAi9utXnQDdww5W",        # Tensor
    "MARKETWwFJX2vVWbZMCCVB8HzYCTJRBQqxrKiMUkMJLz",        # OpenBook / Serum
})


@dataclass
class MonitorConfig:
    poll_interval_active: float = 60.0    # seconds between polls for active wallets
    poll_interval_idle: float = 600.0     # for idle wallets (no new txs in 1h)
    activity_window: int = 3600           # seconds to look back for "active" check
    activity_threshold: int = 3           # new txs in window = active
    min_fee_for_scan: int = DEFAULT_FEE_LAMPORTS + 1  # only scan if fee > default
    max_slots_per_cycle: int = 5          # block scans per poll cycle
    enable_token_boost: bool = True       # boost confidence on shared accounts
    min_shared_tokens: int = 3            # co-purchase threshold to flag a pair
    co_purchase_sweep_interval: int = 300 # seconds between co-purchase sweeps


@dataclass
class NewTxEvent:
    """A new transaction detected for a tracked wallet."""
    address: str
    signature: str
    slot: int
    fee: Optional[int] = None
    block_time: Optional[int] = None
    # {mint: 'buy'|'sell'} — populated from preTokenBalances/postTokenBalances
    token_actions: dict[str, str] = field(default_factory=dict)

    @property
    def token_mints(self) -> list[str]:
        return list(self.token_actions.keys())

    @property
    def sells(self) -> list[str]:
        return [m for m, d in self.token_actions.items() if d == "sell"]

    @property
    def buys(self) -> list[str]:
        return [m for m, d in self.token_actions.items() if d == "buy"]


# Type alias for the candidate callback
CandidateCallback = Callable[[str, list[MatchResult]], Awaitable[None]]


@dataclass(order=True)
class _PollEntry:
    next_poll: float
    address: str = field(compare=False)
    last_sig: Optional[str] = field(compare=False, default=None)


class WalletMonitor:
    """
    Adaptive polling monitor for ~700 tracked wallets.

    Scheduling:
      - Each wallet starts with an idle interval
      - If new transactions are found, it switches to the active interval
      - Slots already in block_cache are skipped (0 extra credits)
      - Block scans triggered only for non-default fees
    """

    def __init__(
        self,
        registry: WalletRegistry,
        client: HeliusClient,
        config: MonitorConfig | None = None,
        on_candidate: CandidateCallback | None = None,
    ):
        self.registry = registry
        self.client = client
        self.cfg = config or MonitorConfig()
        self.on_candidate = on_candidate

        # Priority queue of (next_poll_time, wallet_address)
        self._heap: list[_PollEntry] = []
        self._stop = False

        # Per-wallet last-seen signature (bootstrapped from DB on first run)
        self._last_sig: dict[str, Optional[str]] = {}

        # Pending block scans: slot -> list of (address, fee, sig) triggers
        self._pending_slots: dict[int, list[tuple[str, int, str]]] = defaultdict(list)

        # Track when we last ran a co-purchase sweep
        self._last_co_purchase_sweep: float = 0.0

        # Stats
        self.stats = {
            "polls": 0,
            "new_txs": 0,
            "block_scans": 0,
            "candidates": 0,
            "co_purchase_pairs": 0,
            "credits_used": 0,
        }

    def _build_queue(self, addresses: list[str]):
        """Populate the priority queue from a list of wallet addresses."""
        now = time.monotonic()
        for addr in addresses:
            last = get_last_signature(addr)
            self._last_sig[addr] = last
            # Stagger initial polls to avoid hitting all at once
            offset = len(self._heap) * 0.15
            entry = _PollEntry(
                next_poll=now + offset,
                address=addr,
                last_sig=last,
            )
            heapq.heappush(self._heap, entry)

    async def run_forever(
        self,
        addresses: list[str] | None = None,
        on_tick: Callable[[dict], None] | None = None,
    ):
        """
        Run the monitor indefinitely.

        Args:
            addresses:  Subset of wallet addresses to monitor. None = all.
            on_tick:    Called after each poll cycle with current stats.
        """
        watch = addresses or list(self.registry.all_addresses())
        self._build_queue(watch)
        self._stop = False

        while not self._stop:
            if not self._heap:
                await asyncio.sleep(1)
                continue

            entry = heapq.heappop(self._heap)
            now = time.monotonic()
            wait = entry.next_poll - now
            if wait > 0:
                await asyncio.sleep(min(wait, 1.0))
                heapq.heappush(self._heap, entry)
                if on_tick:
                    on_tick(dict(self.stats))
                continue

            # Budget check
            if self.client.credits_used >= MAX_CREDITS_PER_RUN:
                break

            # Poll this wallet
            new_txs = await self._poll_wallet(entry.address)
            self.stats["polls"] += 1
            self.stats["credits_used"] = self.client.credits_used

            active = len(new_txs) >= self.cfg.activity_threshold
            interval = (
                self.cfg.poll_interval_active if active
                else self.cfg.poll_interval_idle
            )

            # Re-queue with updated interval
            entry.next_poll = time.monotonic() + interval
            heapq.heappush(self._heap, entry)

            # Queue block scans for unique-fee new txs; run sell-cluster scans inline
            sell_candidates: list[MatchResult] = []
            for evt in new_txs:
                # Same-block fee scan (Helius, deferred)
                if evt.fee and evt.fee > self.cfg.min_fee_for_scan:
                    self._pending_slots[evt.slot].append(
                        (evt.address, evt.fee, evt.signature)
                    )

                # Coordinated sell scan (DB-only, 0 credits, immediate)
                if evt.sells and evt.block_time:
                    for mint in evt.sells:
                        hits = coordinated_sell_scan(
                            wallet_address=evt.address,
                            token_mint=mint,
                            block_time=evt.block_time,
                            fee=evt.fee,
                            registry=self.registry,
                        )
                        sell_candidates.extend(hits)

            if sell_candidates and self.on_candidate:
                self.stats["candidates"] += len(sell_candidates)
                await self.on_candidate(entry.address, sell_candidates)

            # Periodic co-purchase pattern sweep (pure DB, 0 credits)
            # Runs every co_purchase_sweep_interval seconds, not every poll.
            now_mono = time.monotonic()
            if (now_mono - self._last_co_purchase_sweep
                    >= self.cfg.co_purchase_sweep_interval):
                self._last_co_purchase_sweep = now_mono
                cp_results = co_purchase_pattern_scan(
                    self.registry,
                    min_shared=self.cfg.min_shared_tokens,
                )
                if cp_results and self.on_candidate:
                    # Group by the first address as "trigger"
                    new_pairs = [r for r in cp_results
                                 if r.confidence >= 0.40]
                    self.stats["co_purchase_pairs"] += len(new_pairs) // 2
                    self.stats["candidates"] += len(new_pairs)
                    if new_pairs:
                        await self.on_candidate("co_purchase_sweep", new_pairs)

            # Run pending block scans (up to cap per iteration)
            scanned = 0
            for slot in list(self._pending_slots.keys()):
                if scanned >= self.cfg.max_slots_per_cycle:
                    break
                triggers = self._pending_slots.pop(slot)
                candidates = await self._scan_block(slot, triggers)
                if candidates:
                    primary_addr = triggers[0][0]
                    self.stats["candidates"] += len(candidates)
                    if self.on_candidate:
                        await self.on_candidate(primary_addr, candidates)
                scanned += 1

            if on_tick:
                on_tick(dict(self.stats))

    def stop(self):
        """Signal the run loop to exit cleanly."""
        self._stop = True

    async def _poll_wallet(self, address: str) -> list[NewTxEvent]:
        """
        Fetch new signatures since last known one.
        Returns list of NewTxEvent for each new transaction.
        1 credit per call.
        """
        try:
            sigs = await self.client.get_signatures(address, limit=20)
        except RuntimeError:
            return []

        if not sigs:
            return []

        last_known = self._last_sig.get(address)
        new = []
        for s in sigs:
            if s.signature == last_known:
                break
            new.append(s)

        if not new:
            return []

        # Update last-known sig and cache
        self._last_sig[address] = sigs[0].signature
        cache_signatures(address, [
            {"signature": s.signature, "slot": s.slot,
             "block_time": s.block_time, "err": s.err}
            for s in new
        ])
        self.stats["new_txs"] += len(new)

        # Fetch full tx data for each new non-error tx.
        # Via free public RPC → 0 Helius credits.
        # Extracts fee (for block scan) + buy/sell actions (for sell-cluster and co-purchase).
        events = []
        for s in new:
            if s.err:
                continue

            fee: Optional[int] = None
            actions: dict[str, str] = {}

            cached = get_cached_tx(s.signature)
            if cached and cached.get("fee") is not None:
                fee = cached["fee"]

            # Fetch the full transaction (free RPC, 0 credits)
            try:
                tx_data = await self.client.get_transaction(s.signature)
            except Exception:
                tx_data = None

            if tx_data:
                meta = tx_data.get("meta") or {}
                if fee is None:
                    fee = meta.get("fee")
                    accounts = (
                        tx_data.get("transaction", {})
                        .get("message", {})
                        .get("accountKeys", [])
                    )
                    fee_payer = None
                    for acc in accounts:
                        if isinstance(acc, dict):
                            if acc.get("signer") and acc.get("writable"):
                                fee_payer = acc.get("pubkey")
                                break
                        elif isinstance(acc, str) and fee_payer is None:
                            fee_payer = acc
                    if fee is not None:
                        cache_tx(s.signature, s.slot, fee, fee_payer or address,
                                 s.block_time)

                # Extract buy/sell direction for each token (free — tx already fetched)
                actions = extract_token_actions(tx_data, address)

                # Save all token interactions with their direction
                if actions and s.block_time:
                    for mint, direction in actions.items():
                        save_token_purchase(
                            address, mint, s.slot, s.block_time,
                            fee, s.signature, direction=direction,
                        )

            events.append(NewTxEvent(
                address=address,
                signature=s.signature,
                slot=s.slot,
                fee=fee,
                block_time=s.block_time,
                token_actions=actions,
            ))

        return events

    async def _scan_block(
        self,
        slot: int,
        triggers: list[tuple[str, int, str]],
    ) -> list[MatchResult]:
        """
        Fetch block at `slot` and find candidates matching any trigger fee.
        Skips if slot is already in block_cache.

        Each trigger is (wallet_address, fee, signature).
        ~10 credits.
        """
        if is_slot_scanned(slot):
            # Re-run matching on cached block data if we have it
            return []

        try:
            block_txs = await self.client.get_block_transactions(slot)
        except RuntimeError:
            return []

        if not block_txs:
            return []

        mark_slot_scanned(slot, len(block_txs))
        self.stats["block_scans"] += 1

        # Build lookup: fee -> list of BlockTx
        by_fee: dict[int, list[BlockTx]] = defaultdict(list)
        for btx in block_txs:
            by_fee[btx.fee].append(btx)

        tracked_addrs = self.registry.all_addresses()
        results: list[MatchResult] = []
        seen_candidates: set[str] = set()

        for (trigger_addr, trigger_fee, trigger_sig) in triggers:
            if trigger_fee not in by_fee:
                continue

            # Get the accounts of the trigger tx for token-match boosting
            trigger_tx = next(
                (b for b in block_txs if b.fee_payer == trigger_addr), None
            )
            trigger_interesting = (
                _interesting_accounts(trigger_tx.accounts)
                if trigger_tx and self.cfg.enable_token_boost
                else frozenset()
            )

            conf_base = _fee_confidence(trigger_fee)

            for btx in by_fee[trigger_fee]:
                if btx.fee_payer == trigger_addr:
                    continue
                if btx.fee_payer in seen_candidates:
                    continue

                # Token / pool co-buyer boost
                shared = (
                    trigger_interesting & _interesting_accounts(btx.accounts)
                    if self.cfg.enable_token_boost
                    else frozenset()
                )
                conf = conf_base
                if shared:
                    # Same pool/token AND same fee in same block = very strong
                    conf = min(conf + 0.15, 0.97)

                label = None
                if btx.fee_payer in tracked_addrs:
                    w = self.registry.get(btx.fee_payer)
                    label = w.label if w else None

                evidence = {
                    "slot": slot,
                    "fee": trigger_fee,
                    "trigger_tx": trigger_sig,
                    "trigger_wallet": trigger_addr,
                    "candidate_tx": btx.signature,
                    "shared_accounts": list(shared),
                    "token_match": bool(shared),
                }

                match = MatchResult(
                    address=btx.fee_payer,
                    match_type="monitor_fee_match",
                    confidence=conf,
                    evidence=evidence,
                    known_label=label,
                )
                results.append(match)
                seen_candidates.add(btx.fee_payer)
                save_candidate(
                    btx.fee_payer, trigger_addr,
                    "monitor_fee_match", conf, evidence,
                )

        return results


def _interesting_accounts(accounts: list[str]) -> frozenset[str]:
    """
    Return accounts that are meaningful for token-match detection.
    Filters out system programs and per-wallet ATAs.
    Keeps DEX programs, pool addresses, and token mint-sized addresses.
    """
    return frozenset(
        a for a in accounts
        if a not in BORING_ACCOUNTS
        # Keep DEX programs explicitly; filter everything else below 40 chars
        # Solana addresses are 43-44 chars; shorter = likely truncated
        and len(a) >= 32
    )
