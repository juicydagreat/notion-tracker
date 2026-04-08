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
