"""
Run this script to import your wallets from the Photon/tracker JSON export.

Usage:
    python scripts/import_wallets.py path/to/your/export.json

Or paste the JSON directly when prompted.
"""
import json
import sys
from pathlib import Path


def main():
    if len(sys.argv) > 1:
        src = Path(sys.argv[1])
        data = json.loads(src.read_text())
    else:
        print("Paste your wallet JSON then press Enter twice:")
        lines = []
        while True:
            line = input()
            if not line and lines:
                break
            lines.append(line)
        data = json.loads("\n".join(lines))

    # Normalise - keep only fields we need
    out = []
    for item in data:
        addr = item.get("trackedWalletAddress", "").strip()
        if not addr:
            continue
        out.append({
            "trackedWalletAddress": addr,
            "name": item.get("name", "?"),
            "emoji": item.get("emoji", ""),
            "alertsOnFeed": item.get("alertsOnFeed", True),
            "alertsOnToast": item.get("alertsOnToast", False),
            "alertsOnBubble": item.get("alertsOnBubble", False),
            "groups": item.get("groups", ["Main"]),
            "sound": item.get("sound", "default"),
        })

    dest = Path("wallets.json")
    dest.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"✓ Imported {len(out)} wallets → wallets.json")

    # Show cluster summary
    from collections import Counter
    names = Counter(w["name"].lower() for w in out)
    clusters = {n: c for n, c in names.items() if c >= 2}
    print(f"\nKnown clusters (same name, 2+ wallets): {len(clusters)}")
    for name, count in sorted(clusters.items(), key=lambda x: -x[1])[:20]:
        print(f"  {name}: {count} wallets")


if __name__ == "__main__":
    main()
