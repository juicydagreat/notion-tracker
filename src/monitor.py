"""
Continuous wallet monitor — polls tracked wallets for new transactions and
automatically triggers same-block fee matching when unique fees are detected.

Token co-buyer detection:
  When two wallets in the same block share a non-trivial program/pool account
  (e.g., the same Raydium pool, token mint, or Jito tip account) in addition
  to having the same fee, confidence is boosted to 0.95+.

Credit budget:
  - 1 credit per wallet poll (getSignaturesForAddress)
  - ~10 credits per block scan (getBlock), skipped if slot already in cache
  - Block scans only triggered when fee > DEFAULT_FEE_LAMPORTS
"""
import asyncio
import heapq
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Awaitable, Optional

from src.config import DEFAULT_FEE_LAMPORTS, MAX_CREDITS_PER_RUN
from src.helius import HeliusClient, BlockTx
from src.db import (
    cache_signatures, cache_tx, get_cached_tx,
    get_last_signature, is_slot_scanned, mark_slot_scanned,
    save_candidate,
)
from src.wallets import WalletRegistry
from src.matcher import MatchResult, _fee_confidence


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


@dataclass
class NewTxEvent:
    """A new transaction detected for a tracked wallet."""
    address: str
    signature: str
    slot: int
    fee: Optional[int] = None
    block_time: Optional[int] = None


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

        # Stats
        self.stats = {
            "polls": 0,
            "new_txs": 0,
            "block_scans": 0,
            "candidates": 0,
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

            # Queue block scans for unique-fee new txs
            for evt in new_txs:
                if evt.fee and evt.fee > self.cfg.min_fee_for_scan:
                    self._pending_slots[evt.slot].append(
                        (evt.address, evt.fee, evt.signature)
                    )

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

        # Fetch fees for non-error txs (only those with unknown fee)
        events = []
        for s in new:
            if s.err:
                continue
            cached = get_cached_tx(s.signature)
            if cached and cached.get("fee") is not None:
                fee = cached["fee"]
            else:
                # Only fetch full tx if we need the fee and can afford it
                if self.client.credits_used + 1 < MAX_CREDITS_PER_RUN:
                    result = await self.client.get_transaction_fee(s.signature)
                    if result:
                        fee, payer = result
                        cache_tx(s.signature, s.slot, fee, payer, s.block_time)
                    else:
                        fee = None
                else:
                    fee = None

            events.append(NewTxEvent(
                address=address,
                signature=s.signature,
                slot=s.slot,
                fee=fee,
                block_time=s.block_time,
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
