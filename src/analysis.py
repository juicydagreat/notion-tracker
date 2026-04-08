"""
Cluster analysis - inspect the existing wallet list for known groupings
and generate insights without burning API credits.
"""
from collections import defaultdict
from src.wallets import WalletRegistry


def print_clusters(registry: WalletRegistry) -> dict:
    """
    Return cluster data grouped by name.
    Already known clusters (same name = likely same person).
    """
    clusters = registry.clusters()
    return {
        name: [w.address for w in wallets]
        for name, wallets in sorted(clusters.items(), key=lambda x: -len(x[1]))
    }


def cluster_by_group(registry: WalletRegistry) -> dict:
    """Return wallets grouped by their tracker group (CHAV, nigas, etc.)."""
    groups: dict[str, list] = defaultdict(list)
    for w in registry.all():
        for g in w.groups:
            groups[g].append(w)
    return dict(groups)


def find_uncertain_attributions(registry: WalletRegistry) -> list:
    """Find wallets with '?' in name - uncertain identity."""
    return [w for w in registry.all() if "?" in w.name]


def summary(registry: WalletRegistry) -> dict:
    clusters = registry.clusters()
    uncertain = find_uncertain_attributions(registry)
    groups = cluster_by_group(registry)

    return {
        "total_wallets": len(registry),
        "unique_names": len(set(w.name.lower() for w in registry.all())),
        "known_clusters": len(clusters),
        "cluster_wallets": sum(len(v) for v in clusters.values()),
        "uncertain": len(uncertain),
        "groups": {g: len(ws) for g, ws in groups.items()},
        "top_clusters": [
            {"name": name, "count": len(wallets), "addresses": [w.address for w in wallets]}
            for name, wallets in sorted(clusters.items(), key=lambda x: -len(x[1]))[:20]
        ],
    }
