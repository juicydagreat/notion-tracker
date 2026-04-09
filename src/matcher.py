"""
Core matching logic:
  1. same_block_fee_scan     - wallets in same block with same fee
  2. co_occurrence_scan      - wallets that repeatedly co-occur in same slots
  3. funding_trace           - SOL inflow chain tracing
  4. temporal_token_scan     - same token bought within a time window (rebuys)
"""
import asyncio
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Optional

from src.config import DEFAULT_FEE_LAMPORTS, MIN_CO_OCCURRENCE
from src.helius import HeliusClient, BlockTx
from src.db import (
    cache_signatures, cache_tx, get_cached_tx,
    get_wallet_slots, get_slots_multi, save_candidate,
    get_token_co_buyers, count_shared_token_purchases,
)
from src.wallets import WalletRegistry


@dataclass
class MatchResult:
    address: str
    match_type: str
    confidence: float
    evidence: dict
    known_label: Optional[str] = None   # if address is already tracked


def _fee_confidence(fee: int) -> float:
    """Higher confidence for non-default fees."""
    if fee == DEFAULT_FEE_LAMPORTS:
        return 0.3  # default fee - could be anyone
    if fee < 10_000:
        return 0.5  # low priority fee
    if fee < 100_000:
        return 0.75
    if fee < 1_000_000:
        return 0.85
    return 0.95   # very high fee = highly unique signature


async def same_block_fee_scan(
    tx_signature: str,
    registry: WalletRegistry,
    client: HeliusClient,
) -> list[MatchResult]:
    """
    Given a specific transaction, scan the block it landed in.
    Find all wallets that paid the exact same fee.
    Returns candidates NOT already in the registry.
    """
    # Try cache first
    cached = get_cached_tx(tx_signature)
    if cached:
        slot = cached["slot"]
        fee = cached["fee"]
        known_fee_payer = cached["fee_payer"]
    else:
        result = await client.get_transaction_fee(tx_signature)
        if not result:
            return []
        fee, known_fee_payer = result
        # Get slot from signatures endpoint
        sigs = await client.get_signatures(known_fee_payer, limit=20)
        slot = next((s.slot for s in sigs if s.signature == tx_signature), None)
        if slot is None:
            return []
        cache_tx(tx_signature, slot, fee, known_fee_payer)

    # Fetch the block (expensive - use sparingly)
    block_txs = await client.get_block_transactions(slot)
    if not block_txs:
        return []

    # Find wallets with matching fee
    results: list[MatchResult] = []
    tracked = registry.all_addresses()
    conf = _fee_confidence(fee)

    for btx in block_txs:
        if btx.fee_payer == known_fee_payer:
            continue
        if btx.fee != fee:
            continue

        evidence = {
            "slot": slot,
            "fee": fee,
            "matched_tx": tx_signature,
            "candidate_tx": btx.signature,
            "default_fee": fee == DEFAULT_FEE_LAMPORTS,
        }

        label = None
        if btx.fee_payer in tracked:
            w = registry.get(btx.fee_payer)
            label = w.label if w else None

        results.append(MatchResult(
            address=btx.fee_payer,
            match_type="same_block_fee",
            confidence=conf,
            evidence=evidence,
            known_label=label,
        ))

        save_candidate(
            btx.fee_payer, known_fee_payer,
            "same_block_fee", conf, evidence,
        )

    return results


async def fetch_and_cache_wallet_sigs(
    address: str,
    client: HeliusClient,
    limit: int = 100,
) -> list[int]:
    """Fetch recent signatures for a wallet and return slot list."""
    sigs = await client.get_signatures(address, limit=limit)
    if not sigs:
        return []
    cache_signatures(address, [
        {"signature": s.signature, "slot": s.slot,
         "block_time": s.block_time, "err": s.err}
        for s in sigs
    ])
    return [s.slot for s in sigs if not s.err]


async def co_occurrence_scan(
    target_addresses: list[str],
    registry: WalletRegistry,
    client: HeliusClient,
    limit: int = 100,
) -> list[MatchResult]:
    """
    For a list of wallets (e.g., a known cluster), fetch their recent
    transaction slots. Find slots where 2+ cluster wallets transacted.
    Then look for OTHER tracked wallets that also hit those slots repeatedly.

    This works entirely from cached data after initial fetch - very credit efficient.
    """
    # Fetch sigs for all target wallets
    all_slot_sets: dict[str, set[int]] = {}
    for addr in target_addresses:
        cached = get_wallet_slots(addr, limit)
        if len(cached) < 10:
            # Fetch from API if cache is thin
            fetched = await fetch_and_cache_wallet_sigs(addr, client, limit)
            all_slot_sets[addr] = set(fetched)
        else:
            all_slot_sets[addr] = set(cached)

    # Find slots where 2+ cluster wallets transacted (hot slots)
    slot_count: Counter = Counter()
    for slots in all_slot_sets.values():
        slot_count.update(slots)

    hot_slots = {slot for slot, count in slot_count.items() if count >= 2}
    if not hot_slots:
        return []

    # Now check ALL other tracked wallets against these hot slots
    all_tracked = registry.all_addresses() - set(target_addresses)

    co_occur: dict[str, int] = defaultdict(int)
    for addr in all_tracked:
        cached = get_wallet_slots(addr, limit)
        if not cached:
            continue
        overlap = len(hot_slots & set(cached))
        if overlap >= MIN_CO_OCCURRENCE:
            co_occur[addr] = overlap

    results: list[MatchResult] = []
    for addr, count in sorted(co_occur.items(), key=lambda x: -x[1]):
        conf = min(0.3 + (count / 10) * 0.5, 0.85)
        w = registry.get(addr)
        evidence = {
            "co_occurring_slots": count,
            "hot_slots_total": len(hot_slots),
            "target_cluster_size": len(target_addresses),
        }
        results.append(MatchResult(
            address=addr,
            match_type="co_occurrence",
            confidence=conf,
            evidence=evidence,
            known_label=w.label if w else None,
        ))
        save_candidate(
            addr, target_addresses[0],
            "co_occurrence", conf, evidence,
        )

    return results


async def funding_trace(
    address: str,
    registry: WalletRegistry,
    client: HeliusClient,
    depth: int = 3,
) -> list[MatchResult]:
    """
    Trace SOL inflows for a wallet back `depth` hops.
    Returns wallets that share common funding ancestors with tracked wallets.
    """
    visited: set[str] = set()
    ancestors: dict[str, list[str]] = {}  # address -> list of ancestor paths

    async def trace(addr: str, path: list[str], remaining: int):
        if remaining == 0 or addr in visited:
            return
        visited.add(addr)

        sigs = await client.get_signatures(addr, limit=50)
        for sig in sigs[:20]:  # limit to save credits
            if sig.err:
                continue
            cached = get_cached_tx(sig.signature)
            if not cached:
                result = await client.get_transaction_fee(sig.signature)
                if result:
                    cache_tx(sig.signature, sig.slot, result[0], result[1],
                             sig.block_time)
                    cached = {"fee_payer": result[1], "slot": sig.slot}

            if not cached:
                continue

            funder = cached.get("fee_payer", "")
            if funder and funder != addr and funder not in visited:
                full_path = path + [funder]
                ancestors[funder] = full_path
                if remaining > 1:
                    await trace(funder, full_path, remaining - 1)

    await trace(address, [address], depth)

    # Find ancestors that are tracked or fund tracked wallets
    results: list[MatchResult] = []
    for ancestor, path in ancestors.items():
        if ancestor in registry.all_addresses():
            conf = max(0.3, 0.9 - (len(path) * 0.15))
            w = registry.get(ancestor)
            evidence = {
                "funding_path": path,
                "hops": len(path),
            }
            results.append(MatchResult(
                address=address,
                match_type="funding",
                confidence=conf,
                evidence=evidence,
                known_label=w.label if w else None,
            ))
            save_candidate(address, ancestor, "funding", conf, evidence)

    return results


def temporal_token_scan(
    trigger_address: str,
    token_mint: str,
    block_time: int,
    fee: Optional[int],
    registry: WalletRegistry,
    window_seconds: int = 300,
) -> list[MatchResult]:
    """
    Find other tracked (or untracked) wallets that bought the same token
    within `window_seconds` of `block_time`.

    This catches the rebuy case: wallet A and an alt both buy the same
    token but a few blocks apart (different slot → missed by same_block_fee_scan).

    Confidence scoring:
      0.25  base  (same token, different block — weak alone)
      +0.35 same fee lamports exactly — near-unique fingerprint
      +0.20 within 60 seconds of each other
      +0.10 within 60–180 seconds
      +0.10 per additional shared token (pattern repeat, capped at +0.30)

    All signals together max out at ~0.95 (fee match + <60s + 3 shared tokens).
    Without fee match, temporal-only matches cap at ~0.65 — saved but flagged
    as low confidence until confirmed by another signal.
    """
    co_buyers = get_token_co_buyers(token_mint, block_time, window_seconds)
    results: list[MatchResult] = []

    for record in co_buyers:
        candidate = record["address"]
        if candidate == trigger_address:
            continue

        conf = 0.25  # base: same token, different block

        # Fee fingerprint match
        if fee is not None and record.get("fee") == fee:
            conf += 0.35

        # Time proximity boost
        time_diff = abs((record.get("block_time") or block_time) - block_time)
        if time_diff <= 60:
            conf += 0.20   # same minute
        elif time_diff <= 180:
            conf += 0.10   # within 3 minutes

        # Pattern boost: how many OTHER tokens have they co-purchased?
        shared = count_shared_token_purchases(
            trigger_address, candidate, window_seconds
        )
        # Don't count the current token (it's what triggered this scan)
        extra = max(shared - 1, 0)
        conf += min(extra * 0.10, 0.30)

        conf = min(round(conf, 2), 0.95)

        w = registry.get(candidate)
        evidence = {
            "token_mint": token_mint,
            "trigger_address": trigger_address,
            "trigger_block_time": block_time,
            "candidate_block_time": record.get("block_time"),
            "time_diff_seconds": time_diff,
            "fee_match": fee is not None and record.get("fee") == fee,
            "trigger_fee": fee,
            "candidate_fee": record.get("fee"),
            "shared_token_count": shared,
            "candidate_sig": record.get("signature"),
            "window_seconds": window_seconds,
        }

        results.append(MatchResult(
            address=candidate,
            match_type="temporal_token",
            confidence=conf,
            evidence=evidence,
            known_label=w.label if w else None,
        ))
        save_candidate(
            candidate, trigger_address,
            "temporal_token", conf, evidence,
        )

    return sorted(results, key=lambda r: -r.confidence)
