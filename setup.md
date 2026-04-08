# Setup

## 1. Install dependencies
```bash
pip install -r requirements.txt
```

## 2. Configure API key
```bash
cp .env.example .env
# Edit .env and add your Helius API key
```

## 3. Import your wallet list
```bash
# From your Photon/tracker export file:
python scripts/import_wallets.py path/to/your/export.json

# Or paste JSON directly:
python scripts/import_wallets.py
```

## 4. Run

### See your existing clusters (no API credits used)
```bash
python discover.py clusters
```

### Find wallets in same block with same fee as a known transaction
```bash
python discover.py scan-block <tx_signature>
```

### Co-occurrence scan for a named cluster
```bash
# First refresh the tx cache (uses ~1 credit per wallet)
python discover.py refresh brad

# Then scan
python discover.py scan-cluster brad
```

### Trace funding chain
```bash
python discover.py trace <wallet_address>
```

### View all saved candidates
```bash
python discover.py candidates
python discover.py candidates 0.7  # min 70% confidence
```

### Export a cluster as importable JSON
```bash
# Export known cluster — prints JSON to stdout
python discover.py export brad

# Include newly discovered addresses in the export
python discover.py export brad <addr1> <addr2>

# Save directly to a file
python discover.py export brad --out brad_export.json

# Merge straight into wallets.json (updates existing + appends new)
python discover.py export brad --merge

# Skip Twitter search (faster, saves time when offline)
python discover.py export brad --no-twitter --merge
```

The export applies the naming convention automatically:
- **`BRAD`** (UPPERCASE) → main wallet (KolScan leaderboard first, else highest GMGN PnL)
- **`brad`** (lowercase) → all alt wallets

The output JSON is in the exact Photon terminal import format and can be pasted
directly back into your tracker.

## Credit Usage Guide

| Command | Credits | Notes |
|---------|---------|-------|
| `clusters` | 0 | Reads local file only |
| `refresh <name>` | ~1/wallet | Caches tx signatures |
| `scan-block` | ~12 | 1 for tx + ~10 for block |
| `scan-cluster` | 0 (cached) | Uses local DB after refresh |
| `trace` | ~5-20 | Depends on depth |

**Budget:** Set `MAX_CREDITS_PER_RUN` in `.env` to limit spending per session.
