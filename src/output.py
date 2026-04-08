"""
Output formatter — converts discovered wallet clusters into the Photon/KolScan
terminal import format.

Naming convention:
  UPPERCASE  →  main/primary wallet (listed on KolScan or highest PnL)
  lowercase  →  alt wallets

The returned list can be directly merged into wallets.json and re-imported
into the terminal tracker.
"""
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from src.wallets import TrackedWallet, WalletRegistry
from src.kolscan import identify_main_wallet, get_leaderboard_name
from src.gmgn import get_pnl_batch, wallet_page_url as gmgn_url
from src.twitter import search_wallets_batch, TwitterHit


@dataclass
class ClusterExportResult:
    name: str                              # Resolved base name (mixed case from input)
    main_address: Optional[str]            # The primary wallet (UPPERCASE)
    alt_addresses: list[str]               # Alt wallets (lowercase)
    new_addresses: list[str]               # Newly discovered, not yet tracked
    twitter_hits: dict[str, TwitterHit] = field(default_factory=dict)
    pnl: dict[str, Optional[float]] = field(default_factory=dict)
    main_source: str = "unknown"           # "kolscan" | "gmgn_pnl" | "fallback"

    @property
    def all_addresses(self) -> list[str]:
        out = []
        if self.main_address:
            out.append(self.main_address)
        out.extend(self.alt_addresses)
        out.extend(self.new_addresses)
        return out


def _photon_entry(
    address: str,
    name: str,
    *,
    emoji: str = "",
    groups: list[str] | None = None,
    alerts_on_feed: bool = True,
) -> dict:
    """Build a single Photon-format wallet entry."""
    return {
        "trackedWalletAddress": address,
        "name": name,
        "emoji": emoji,
        "alertsOnFeed": alerts_on_feed,
        "alertsOnToast": False,
        "alertsOnBubble": False,
        "groups": groups or ["Main"],
        "sound": "default",
    }


async def resolve_cluster(
    base_name: str,
    addresses: list[str],
    registry: WalletRegistry,
    *,
    new_addresses: list[str] | None = None,
    run_twitter: bool = True,
) -> ClusterExportResult:
    """
    Determine which wallet is the "main" and format the full cluster.

    Priority:
      1. KolScan leaderboard — that wallet is the main
      2. Highest realized PnL from GMGN — that wallet is the main
      3. Fallback — first address in list is main

    Args:
        base_name:     The cluster name as stored (e.g. "brad" or "BRAD")
        addresses:     All known addresses for this cluster
        registry:      Existing wallet registry (to preserve metadata)
        new_addresses: Newly discovered addresses not yet in registry
        run_twitter:   Whether to query Twitter for identity hints
    """
    all_addrs = list(addresses) + (new_addresses or [])
    result = ClusterExportResult(
        name=base_name,
        main_address=None,
        alt_addresses=[],
        new_addresses=new_addresses or [],
    )

    # Step 1: KolScan check
    main = await identify_main_wallet(all_addrs)
    if main:
        result.main_address = main
        result.main_source = "kolscan"

    # Step 2: GMGN PnL fallback
    if not result.main_address:
        pnl_map = await get_pnl_batch(all_addrs)
        result.pnl = pnl_map
        ranked = sorted(
            [(a, p) for a, p in pnl_map.items() if p is not None],
            key=lambda x: x[1],
            reverse=True,
        )
        if ranked:
            result.main_address = ranked[0][0]
            result.main_source = "gmgn_pnl"

    # Step 3: Fallback — first address
    if not result.main_address and all_addrs:
        result.main_address = all_addrs[0]
        result.main_source = "fallback"

    result.alt_addresses = [a for a in addresses if a != result.main_address]

    # Step 4: Twitter identity hints
    if run_twitter:
        result.twitter_hits = await search_wallets_batch(all_addrs)

    return result


def format_as_importable(
    result: ClusterExportResult,
    registry: WalletRegistry,
) -> list[dict]:
    """
    Convert a ClusterExportResult into a list of Photon-format wallet dicts
    ready to be merged into wallets.json.

    - Main wallet:  name = BASE_NAME (UPPERCASE)
    - Alt wallets:  name = base_name (lowercase)
    - New wallets:  name = base_name (lowercase), marked with group "Discovered"
    """
    base = result.name.lower()
    main_name = base.upper()
    alt_name = base

    entries = []

    def _preserve(address: str, override_name: str) -> dict:
        """Use existing registry metadata where available, override name."""
        existing = registry.get(address)
        if existing:
            return _photon_entry(
                address,
                override_name,
                emoji=existing.emoji,
                groups=existing.groups,
                alerts_on_feed=existing.alerts_on_feed,
            )
        return _photon_entry(address, override_name)

    # Main wallet
    if result.main_address:
        entries.append(_preserve(result.main_address, main_name))

    # Known alt wallets
    for addr in result.alt_addresses:
        entries.append(_preserve(addr, alt_name))

    # Newly discovered wallets
    for addr in result.new_addresses:
        tw = result.twitter_hits.get(addr)
        # Prefer Twitter-suggested name if available, otherwise use base alt name
        name = alt_name
        entry = _photon_entry(addr, name, groups=["Discovered"])
        entries.append(entry)

    return entries


async def export_cluster(
    cluster_name: str,
    registry: WalletRegistry,
    new_addresses: list[str] | None = None,
    run_twitter: bool = True,
) -> tuple[ClusterExportResult, list[dict]]:
    """
    Full pipeline for a named cluster:
      1. Look up existing wallets for the cluster
      2. Resolve main via KolScan → GMGN → fallback
      3. Format as importable JSON
      4. Return both the result metadata and the formatted entries

    Args:
        cluster_name:  Cluster name to look up (case-insensitive)
        registry:      Loaded WalletRegistry
        new_addresses: Extra addresses found by scanning (not yet tracked)
        run_twitter:   Whether to run Twitter identity search
    """
    wallets = registry.by_name(cluster_name)
    addresses = [w.address for w in wallets]

    result = await resolve_cluster(
        cluster_name,
        addresses,
        registry,
        new_addresses=new_addresses,
        run_twitter=run_twitter,
    )
    entries = format_as_importable(result, registry)
    return result, entries


def write_export(entries: list[dict], path: str | Path) -> None:
    """Write the formatted entries to a JSON file."""
    path = Path(path)
    path.write_text(json.dumps(entries, indent=2, ensure_ascii=False))


def merge_into_wallets_json(
    new_entries: list[dict],
    wallets_path: str | Path = "wallets.json",
) -> tuple[int, int]:
    """
    Merge new entries into wallets.json, updating existing records and
    appending new ones.

    Returns (updated_count, added_count).
    """
    wallets_path = Path(wallets_path)
    existing: list[dict] = json.loads(wallets_path.read_text()) if wallets_path.exists() else []

    existing_by_addr = {w["trackedWalletAddress"]: i for i, w in enumerate(existing)}
    updated = 0
    added = 0

    for entry in new_entries:
        addr = entry["trackedWalletAddress"]
        if addr in existing_by_addr:
            existing[existing_by_addr[addr]] = entry
            updated += 1
        else:
            existing.append(entry)
            added += 1

    wallets_path.write_text(json.dumps(existing, indent=2, ensure_ascii=False))
    return updated, added
