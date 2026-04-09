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
| `export` | 0 | No Helius calls needed |
| `daemon` (idle) | ~1/wallet/cycle | No block scans until unique fee seen |
| `daemon` (active) | +10/block scan | Only for non-default fees, cached after first scan |

**Budget:** Set `MAX_CREDITS_PER_RUN` in `.env` to limit spending per session.

---

## Continuous Discovery Daemon

Runs in the background, watches all your tracked wallets for new transactions,
and automatically identifies alt wallets when unique fee fingerprints appear.

### How it works

```
Tracked wallet makes a tx with fee 42,000 lamports
  → daemon detects new tx within 60 seconds
  → fetches the block (~10 credits, cached forever after)
  → scans all block txs for same fee (42,000)
  → if candidate also touched the same Raydium pool/token mint → confidence boosted
  → saves to discovery.db
```

### Run the daemon

```bash
# Monitor all wallets (recommended: set --max-credits to stay in budget)
python daemon.py --max-credits 300

# Only watch a specific cluster
python daemon.py --cluster brad --max-credits 100

# Faster polling for active sessions
python daemon.py --interval 30 --idle-interval 300

# Disable token-account confidence boost
python daemon.py --no-token-boost
```

### Daemon flags

| Flag | Default | Description |
|------|---------|-------------|
| `--cluster <name>` | all wallets | Only monitor this cluster |
| `--max-credits N` | from .env | Stop after N Helius credits |
| `--interval N` | 60s | Poll interval for active wallets |
| `--idle-interval N` | 600s | Poll interval for idle wallets |
| `--no-token-boost` | boost on | Disable shared-account confidence boost |
| `--min-confidence F` | 0.5 | Minimum confidence for display |

### Credit budget planning

With 700 wallets and default intervals:
- Active wallets (recent tx): polled every 60s → ~1 credit/min each
- Idle wallets: polled every 600s → very low cost
- Block scans: only trigger on non-default fees, cached → typically 10-20 credits/hour
- **Recommended**: `--max-credits 200` for a safe hourly budget

### Workflow

```bash
# 1. Import your wallets
python scripts/import_wallets.py path/to/export.json

# 2. Do an initial cache warm-up (run once)
python discover.py refresh all   # or refresh by cluster

# 3. Run the daemon
python daemon.py --max-credits 200

# 4. Check what was found (in another terminal)
python discover.py candidates 0.7

# 5. Export a confirmed cluster to importable JSON
python discover.py export brad --merge
```

