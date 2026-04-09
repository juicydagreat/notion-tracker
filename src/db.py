"""
SQLite cache for transaction data and discovered candidates.
Avoids re-fetching data and burning Helius credits.
"""
import sqlite3
import json
import time
from contextlib import contextmanager
from pathlib import Path

from src.config import DB_PATH


def init_db(path: str = DB_PATH):
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS tx_cache (
            signature   TEXT PRIMARY KEY,
            slot        INTEGER NOT NULL,
            fee         INTEGER,
            fee_payer   TEXT,
            block_time  INTEGER,
            fetched_at  INTEGER NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_tx_slot ON tx_cache(slot);
        CREATE INDEX IF NOT EXISTS idx_tx_fee_payer ON tx_cache(fee_payer);

        CREATE TABLE IF NOT EXISTS wallet_sigs (
            address     TEXT NOT NULL,
            signature   TEXT NOT NULL,
            slot        INTEGER NOT NULL,
            block_time  INTEGER,
            err         INTEGER DEFAULT 0,
            fetched_at  INTEGER NOT NULL,
            PRIMARY KEY (address, signature)
        );

        CREATE INDEX IF NOT EXISTS idx_wsigs_slot ON wallet_sigs(slot);

        CREATE TABLE IF NOT EXISTS block_cache (
            slot        INTEGER PRIMARY KEY,
            tx_count    INTEGER,
            fetched_at  INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS candidates (
            address         TEXT NOT NULL,
            matched_wallet  TEXT NOT NULL,
            match_type      TEXT NOT NULL,   -- 'same_block_fee', 'co_occurrence', 'funding'
            confidence      REAL NOT NULL,   -- 0.0 - 1.0
            evidence        TEXT,            -- JSON blob
            first_seen      INTEGER NOT NULL,
            last_seen       INTEGER NOT NULL,
            confirmed       INTEGER DEFAULT 0,
            PRIMARY KEY (address, matched_wallet, match_type)
        );

        CREATE TABLE IF NOT EXISTS known_clusters (
            cluster_name    TEXT NOT NULL,
            address         TEXT NOT NULL,
            source          TEXT NOT NULL,   -- 'manual', 'discovered'
            added_at        INTEGER NOT NULL,
            PRIMARY KEY (cluster_name, address)
        );

        -- Every token purchase we observe for any tracked wallet.
        -- Used for temporal co-purchase matching (same token, different block).
        CREATE TABLE IF NOT EXISTS token_purchases (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            address     TEXT NOT NULL,        -- wallet that bought
            token_mint  TEXT NOT NULL,        -- token mint address
            slot        INTEGER NOT NULL,
            block_time  INTEGER,
            fee         INTEGER,              -- lamports (for fee-pattern boost)
            signature   TEXT NOT NULL,
            fetched_at  INTEGER NOT NULL,
            UNIQUE (address, signature, token_mint)
        );

        CREATE INDEX IF NOT EXISTS idx_tp_mint_time
            ON token_purchases(token_mint, block_time);
        CREATE INDEX IF NOT EXISTS idx_tp_address
            ON token_purchases(address);
    """)
    con.commit()
    con.close()


@contextmanager
def get_db(path: str = DB_PATH):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def cache_signatures(address: str, sigs: list[dict], path: str = DB_PATH):
    now = int(time.time())
    with get_db(path) as db:
        db.executemany(
            """INSERT OR REPLACE INTO wallet_sigs
               (address, signature, slot, block_time, err, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [
                (address, s["signature"], s["slot"],
                 s.get("block_time"), 1 if s.get("err") else 0, now)
                for s in sigs
            ],
        )


def cache_tx(sig: str, slot: int, fee: int, fee_payer: str,
             block_time: int = None, path: str = DB_PATH):
    now = int(time.time())
    with get_db(path) as db:
        db.execute(
            """INSERT OR REPLACE INTO tx_cache
               (signature, slot, fee, fee_payer, block_time, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (sig, slot, fee, fee_payer, block_time, now),
        )


def get_cached_tx(sig: str, path: str = DB_PATH) -> dict | None:
    with get_db(path) as db:
        row = db.execute(
            "SELECT * FROM tx_cache WHERE signature = ?", (sig,)
        ).fetchone()
        return dict(row) if row else None


def get_wallet_slots(address: str, limit: int = 200,
                     path: str = DB_PATH) -> list[int]:
    """Get cached slots for a wallet (no API call)."""
    with get_db(path) as db:
        rows = db.execute(
            """SELECT slot FROM wallet_sigs
               WHERE address = ? AND err = 0
               ORDER BY slot DESC LIMIT ?""",
            (address, limit),
        ).fetchall()
        return [r["slot"] for r in rows]


def get_slots_multi(addresses: list[str], limit: int = 200,
                    path: str = DB_PATH) -> dict[str, list[int]]:
    """Get cached slots for multiple wallets."""
    result = {}
    for addr in addresses:
        result[addr] = get_wallet_slots(addr, limit, path)
    return result


def save_candidate(address: str, matched_wallet: str, match_type: str,
                   confidence: float, evidence: dict, path: str = DB_PATH):
    now = int(time.time())
    with get_db(path) as db:
        db.execute(
            """INSERT INTO candidates
               (address, matched_wallet, match_type, confidence, evidence,
                first_seen, last_seen)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(address, matched_wallet, match_type) DO UPDATE SET
                 confidence = MAX(confidence, excluded.confidence),
                 last_seen = excluded.last_seen,
                 evidence = excluded.evidence""",
            (address, matched_wallet, match_type, confidence,
             json.dumps(evidence), now, now),
        )


def get_candidates(min_confidence: float = 0.5,
                   path: str = DB_PATH) -> list[dict]:
    with get_db(path) as db:
        rows = db.execute(
            """SELECT * FROM candidates
               WHERE confirmed = 0 AND confidence >= ?
               ORDER BY confidence DESC, last_seen DESC""",
            (min_confidence,),
        ).fetchall()
        return [dict(r) for r in rows]


def mark_confirmed(address: str, path: str = DB_PATH):
    with get_db(path) as db:
        db.execute(
            "UPDATE candidates SET confirmed = 1 WHERE address = ?",
            (address,),
        )


# ── Monitor helpers ──────────────────────────────────────────────────────────

def get_last_signature(address: str, path: str = DB_PATH) -> str | None:
    """Return the most recently cached signature for a wallet (highest slot)."""
    with get_db(path) as db:
        row = db.execute(
            """SELECT signature FROM wallet_sigs
               WHERE address = ? ORDER BY slot DESC LIMIT 1""",
            (address,),
        ).fetchone()
        return row["signature"] if row else None


def is_slot_scanned(slot: int, path: str = DB_PATH) -> bool:
    """Return True if we've already fetched this block's transactions."""
    with get_db(path) as db:
        row = db.execute(
            "SELECT 1 FROM block_cache WHERE slot = ?", (slot,)
        ).fetchone()
        return row is not None


def mark_slot_scanned(slot: int, tx_count: int = 0, path: str = DB_PATH):
    """Record that we've fully scanned this block slot."""
    now = int(time.time())
    with get_db(path) as db:
        db.execute(
            """INSERT OR REPLACE INTO block_cache (slot, tx_count, fetched_at)
               VALUES (?, ?, ?)""",
            (slot, tx_count, now),
        )


def get_recent_candidates(since_ts: int, path: str = DB_PATH) -> list[dict]:
    """Return candidates discovered after `since_ts` (Unix timestamp)."""
    with get_db(path) as db:
        rows = db.execute(
            """SELECT * FROM candidates
               WHERE first_seen >= ? ORDER BY first_seen DESC""",
            (since_ts,),
        ).fetchall()
        return [dict(r) for r in rows]


def count_recent_txs(address: str, since_ts: int,
                     path: str = DB_PATH) -> int:
    """Count cached txs for a wallet after a given timestamp."""
    with get_db(path) as db:
        row = db.execute(
            """SELECT COUNT(*) as n FROM wallet_sigs
               WHERE address = ? AND block_time >= ? AND err = 0""",
            (address, since_ts),
        ).fetchone()
        return row["n"] if row else 0


# ── Token purchase helpers ────────────────────────────────────────────────────

def save_token_purchase(
    address: str, token_mint: str, slot: int,
    block_time: int | None, fee: int | None, signature: str,
    path: str = DB_PATH,
):
    """Record a token purchase for later temporal co-purchase matching."""
    now = int(time.time())
    with get_db(path) as db:
        db.execute(
            """INSERT OR IGNORE INTO token_purchases
               (address, token_mint, slot, block_time, fee, signature, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (address, token_mint, slot, block_time, fee, signature, now),
        )


def get_token_co_buyers(
    token_mint: str,
    block_time: int,
    window_seconds: int = 300,
    path: str = DB_PATH,
) -> list[dict]:
    """
    Return all recorded purchases of `token_mint` within ±window_seconds
    of `block_time`, by any tracked wallet.
    """
    lo = block_time - window_seconds
    hi = block_time + window_seconds
    with get_db(path) as db:
        rows = db.execute(
            """SELECT address, token_mint, slot, block_time, fee, signature
               FROM token_purchases
               WHERE token_mint = ?
                 AND block_time BETWEEN ? AND ?
               ORDER BY ABS(block_time - ?) ASC""",
            (token_mint, lo, hi, block_time),
        ).fetchall()
        return [dict(r) for r in rows]


def count_shared_token_purchases(
    addr1: str, addr2: str,
    window_seconds: int = 300,
    path: str = DB_PATH,
) -> int:
    """
    Count how many distinct tokens addr1 and addr2 have both purchased
    within `window_seconds` of each other.  Used to boost confidence when
    the same pair repeatedly buys the same tokens around the same time.
    """
    with get_db(path) as db:
        row = db.execute(
            """
            SELECT COUNT(DISTINCT a.token_mint) as n
            FROM token_purchases a
            JOIN token_purchases b
              ON  a.token_mint = b.token_mint
              AND ABS(a.block_time - b.block_time) <= ?
            WHERE a.address = ?
              AND b.address = ?
            """,
            (window_seconds, addr1, addr2),
        ).fetchone()
        return row["n"] if row else 0

