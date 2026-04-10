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

        -- Every token interaction we observe for any tracked wallet.
        -- direction: 'buy' | 'sell' | 'unknown'
        CREATE TABLE IF NOT EXISTS token_purchases (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            address     TEXT NOT NULL,
            token_mint  TEXT NOT NULL,
            slot        INTEGER NOT NULL,
            block_time  INTEGER,
            fee         INTEGER,
            direction   TEXT NOT NULL DEFAULT 'unknown',
            signature   TEXT NOT NULL,
            fetched_at  INTEGER NOT NULL,
            UNIQUE (address, signature, token_mint)
        );

        CREATE INDEX IF NOT EXISTS idx_tp_mint_time
            ON token_purchases(token_mint, block_time);
        CREATE INDEX IF NOT EXISTS idx_tp_address
            ON token_purchases(address);
        CREATE INDEX IF NOT EXISTS idx_tp_direction
            ON token_purchases(direction, token_mint, block_time);

        -- Copy bot lead tracking: records where a wallet bought BEFORE a known copy bot.
        -- Each row = one token where lead_wallet preceded bot_wallet by lag_seconds.
        -- Accumulates over time; use get_bot_leads_ranked() to surface the pattern.
        CREATE TABLE IF NOT EXISTS bot_leads (
            bot_wallet      TEXT NOT NULL,
            lead_wallet     TEXT NOT NULL,
            token_mint      TEXT NOT NULL,
            lead_block_time INTEGER NOT NULL,
            bot_block_time  INTEGER NOT NULL,
            lag_seconds     INTEGER NOT NULL,
            lead_sig        TEXT,
            bot_sig         TEXT,
            recorded_at     INTEGER NOT NULL,
            PRIMARY KEY (bot_wallet, lead_wallet, token_mint)
        );

        CREATE INDEX IF NOT EXISTS idx_bl_bot  ON bot_leads(bot_wallet);
        CREATE INDEX IF NOT EXISTS idx_bl_lead ON bot_leads(lead_wallet);
    """)
    # Migration: add direction column to existing DBs that predate this schema
    try:
        con.execute(
            "ALTER TABLE token_purchases ADD COLUMN direction TEXT NOT NULL DEFAULT 'unknown'"
        )
        con.commit()
    except Exception:
        pass  # column already exists
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
    direction: str = "unknown",
    path: str = DB_PATH,
):
    """Record a token interaction (buy or sell) for co-purchase / sell-cluster matching."""
    now = int(time.time())
    with get_db(path) as db:
        db.execute(
            """INSERT OR IGNORE INTO token_purchases
               (address, token_mint, slot, block_time, fee, direction, signature, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (address, token_mint, slot, block_time, fee, direction, signature, now),
        )


def get_token_buyers(token_mint: str, path: str = DB_PATH) -> list[dict]:
    """
    Return all wallets we've recorded buying `token_mint`, across all time.
    Used to populate a token's buyer set for pattern matching.
    """
    with get_db(path) as db:
        rows = db.execute(
            """SELECT address, slot, block_time, fee, signature
               FROM token_purchases
               WHERE token_mint = ?
               ORDER BY slot ASC""",
            (token_mint,),
        ).fetchall()
        return [dict(r) for r in rows]


def count_shared_token_purchases(
    addr1: str, addr2: str,
    path: str = DB_PATH,
) -> int:
    """
    Count how many distinct tokens both addr1 and addr2 have ever purchased.
    No time window — consistency over the entire history is what matters.
    """
    with get_db(path) as db:
        row = db.execute(
            """
            SELECT COUNT(DISTINCT a.token_mint) as n
            FROM token_purchases a
            JOIN token_purchases b ON a.token_mint = b.token_mint
            WHERE a.address = ? AND b.address = ?
            """,
            (addr1, addr2),
        ).fetchone()
        return row["n"] if row else 0


def get_co_purchase_pairs(
    min_shared: int = 3,
    path: str = DB_PATH,
) -> list[dict]:
    """
    Find all wallet pairs that have purchased min_shared or more tokens
    in common — across the full history, regardless of timing.

    Returns rows: {addr1, addr2, shared_tokens, token_mints (comma-sep),
                   fee_matches (count of purchases with same fee)}

    This is the core "bucket" query: any pair above min_shared is a candidate
    for being the same person operating multiple wallets.
    """
    with get_db(path) as db:
        rows = db.execute(
            """
            SELECT
                a.address                           AS addr1,
                b.address                           AS addr2,
                COUNT(DISTINCT a.token_mint)        AS shared_tokens,
                GROUP_CONCAT(DISTINCT a.token_mint) AS token_mints,
                SUM(CASE WHEN a.fee = b.fee
                         AND a.fee IS NOT NULL THEN 1 ELSE 0 END) AS fee_matches
            FROM token_purchases a
            JOIN token_purchases b ON a.token_mint = b.token_mint
            WHERE a.address < b.address
            GROUP BY a.address, b.address
            HAVING shared_tokens >= ?
            ORDER BY shared_tokens DESC, fee_matches DESC
            """,
            (min_shared,),
        ).fetchall()
        return [dict(r) for r in rows]


# ── Coordinated sell helpers ──────────────────────────────────────────────────

def get_coordinated_sell_partners(
    token_mint: str,
    block_time: int,
    exclude_address: str,
    window_seconds: int = 10,
    path: str = DB_PATH,
) -> list[dict]:
    """
    Find all wallets that sold `token_mint` within ±window_seconds of block_time.
    This detects the 'select all → sell all' pattern.

    Returns [{address, slot, block_time, fee, signature, time_diff_seconds}]
    sorted by closeness in time (tightest first).
    """
    lo = block_time - window_seconds
    hi = block_time + window_seconds
    with get_db(path) as db:
        rows = db.execute(
            """
            SELECT
                address, slot, block_time, fee, signature,
                ABS(block_time - ?) AS time_diff_seconds
            FROM token_purchases
            WHERE token_mint   = ?
              AND direction    = 'sell'
              AND block_time   BETWEEN ? AND ?
              AND address     != ?
            ORDER BY time_diff_seconds ASC
            """,
            (block_time, token_mint, lo, hi, exclude_address),
        ).fetchall()
        return [dict(r) for r in rows]


def count_coordinated_sells(
    addr1: str,
    addr2: str,
    window_seconds: int = 10,
    path: str = DB_PATH,
) -> int:
    """
    Count how many distinct tokens addr1 and addr2 have BOTH sold within
    window_seconds of each other.  A high count = very strong ownership signal.
    """
    with get_db(path) as db:
        row = db.execute(
            """
            SELECT COUNT(DISTINCT a.token_mint) AS n
            FROM token_purchases a
            JOIN token_purchases b
              ON  a.token_mint = b.token_mint
              AND ABS(a.block_time - b.block_time) <= ?
            WHERE a.address   = ?
              AND b.address   = ?
              AND a.direction = 'sell'
              AND b.direction = 'sell'
            """,
            (window_seconds, addr1, addr2),
        ).fetchone()
        return row["n"] if row else 0


def get_sell_cluster_pairs(
    window_seconds: int = 10,
    min_co_sells: int = 1,
    path: str = DB_PATH,
) -> list[dict]:
    """
    Find ALL wallet pairs that have sold the same token within window_seconds
    of each other at least min_co_sells times.

    Returns [{addr1, addr2, co_sell_count, token_mints, avg_time_diff_seconds}]
    sorted by co_sell_count DESC, avg_time_diff ASC.
    """
    with get_db(path) as db:
        rows = db.execute(
            """
            SELECT
                a.address                           AS addr1,
                b.address                           AS addr2,
                COUNT(DISTINCT a.token_mint)        AS co_sell_count,
                GROUP_CONCAT(DISTINCT a.token_mint) AS token_mints,
                AVG(ABS(a.block_time - b.block_time)) AS avg_time_diff_seconds,
                SUM(CASE WHEN a.fee = b.fee
                         AND a.fee IS NOT NULL THEN 1 ELSE 0 END) AS fee_matches
            FROM token_purchases a
            JOIN token_purchases b
              ON  a.token_mint = b.token_mint
              AND a.direction  = 'sell'
              AND b.direction  = 'sell'
              AND ABS(a.block_time - b.block_time) <= ?
              AND a.address    < b.address
            GROUP BY a.address, b.address
            HAVING co_sell_count >= ?
            ORDER BY co_sell_count DESC, avg_time_diff_seconds ASC
            """,
            (window_seconds, min_co_sells),
        ).fetchall()
        return [dict(r) for r in rows]


# ── Bot lead helpers ─────────────────────────────────────────────────────────

def save_bot_lead(
    bot_wallet: str,
    lead_wallet: str,
    token_mint: str,
    lead_block_time: int,
    bot_block_time: int,
    lag_seconds: int,
    lead_sig: str | None = None,
    bot_sig: str | None = None,
    path: str = DB_PATH,
):
    """Record that lead_wallet bought token_mint lag_seconds before bot_wallet."""
    now = int(time.time())
    with get_db(path) as db:
        db.execute(
            """INSERT OR REPLACE INTO bot_leads
               (bot_wallet, lead_wallet, token_mint, lead_block_time,
                bot_block_time, lag_seconds, lead_sig, bot_sig, recorded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (bot_wallet, lead_wallet, token_mint, lead_block_time,
             bot_block_time, lag_seconds, lead_sig, bot_sig, now),
        )


def get_bot_leads_ranked(bot_wallet: str, path: str = DB_PATH) -> list[dict]:
    """
    Return all wallets that preceded bot_wallet, ranked by how many tokens
    they led on (descending) and average lag (ascending).
    """
    with get_db(path) as db:
        rows = db.execute(
            """
            SELECT
                lead_wallet,
                COUNT(DISTINCT token_mint)              AS tokens_led,
                AVG(lag_seconds)                        AS avg_lag_seconds,
                MIN(lag_seconds)                        AS min_lag_seconds,
                GROUP_CONCAT(DISTINCT token_mint)       AS token_mints
            FROM bot_leads
            WHERE bot_wallet = ?
            GROUP BY lead_wallet
            ORDER BY tokens_led DESC, avg_lag_seconds ASC
            """,
            (bot_wallet,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_lead_bot_map(lead_wallet: str, path: str = DB_PATH) -> list[dict]:
    """
    Reverse lookup: which bots has this wallet been leading?
    Useful for confirming a wallet is being systematically copied.
    """
    with get_db(path) as db:
        rows = db.execute(
            """
            SELECT
                bot_wallet,
                COUNT(DISTINCT token_mint)              AS tokens_led,
                AVG(lag_seconds)                        AS avg_lag_seconds
            FROM bot_leads
            WHERE lead_wallet = ?
            GROUP BY bot_wallet
            ORDER BY tokens_led DESC
            """,
            (lead_wallet,),
        ).fetchall()
        return [dict(r) for r in rows]
