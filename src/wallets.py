"""
Wallet list management - load, cluster, query.
"""
import json
from dataclasses import dataclass, field
from pathlib import Path
from collections import defaultdict
from typing import Optional

from src.config import WALLETS_FILE


@dataclass
class TrackedWallet:
    address: str
    name: str
    emoji: str
    groups: list[str] = field(default_factory=list)
    alerts_on_feed: bool = True

    @property
    def label(self) -> str:
        return f"{self.emoji} {self.name}"

    @property
    def display(self) -> str:
        return f"{self.emoji} {self.name} ({self.address[:8]}...)"


class WalletRegistry:
    def __init__(self, path: str = WALLETS_FILE):
        self._wallets: dict[str, TrackedWallet] = {}
        self._by_name: dict[str, list[TrackedWallet]] = defaultdict(list)
        self._load(path)

    def _load(self, path: str):
        data = json.loads(Path(path).read_text())
        for item in data:
            addr = item["trackedWalletAddress"].strip()
            w = TrackedWallet(
                address=addr,
                name=item.get("name", "?"),
                emoji=item.get("emoji", ""),
                groups=item.get("groups", []),
                alerts_on_feed=item.get("alertsOnFeed", True),
            )
            self._wallets[addr] = w
            self._by_name[w.name.lower()].append(w)

    def get(self, address: str) -> Optional[TrackedWallet]:
        return self._wallets.get(address)

    def is_tracked(self, address: str) -> bool:
        return address in self._wallets

    def all(self) -> list[TrackedWallet]:
        return list(self._wallets.values())

    # Alias used by daemon / refresh-all workflows
    all_wallets = all

    def all_addresses(self) -> set[str]:
        return set(self._wallets.keys())

    def by_name(self, name: str) -> list[TrackedWallet]:
        return self._by_name.get(name.lower(), [])

    def by_group(self, group: str) -> list[TrackedWallet]:
        return [w for w in self._wallets.values() if group in w.groups]

    def clusters(self) -> dict[str, list[TrackedWallet]]:
        """Return name clusters with 2+ wallets (same name = likely same person)."""
        return {
            name: wallets
            for name, wallets in self._by_name.items()
            if len(wallets) >= 2
        }

    def search(self, query: str) -> list[TrackedWallet]:
        """Search by name or partial address."""
        q = query.lower()
        return [
            w for w in self._wallets.values()
            if q in w.name.lower() or q in w.address.lower()
        ]

    def __len__(self) -> int:
        return len(self._wallets)
