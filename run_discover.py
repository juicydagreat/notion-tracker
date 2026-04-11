#!/usr/bin/env python3
"""
Wallet Discovery CI Runner
──────────────────────────
Designed to be called by the GitHub Actions workflow. Can also be run
locally. Reads all config from environment variables, runs the investigation,
and optionally writes results to Notion.

Environment variables (all optional except HELIUS_API_KEY):
  HELIUS_API_KEY        Your Helius API key
  FREE_RPC_URL          Free public RPC (default: mainnet-beta)
  NOTION_TOKEN          Notion integration secret (required for Notion output)
  NOTION_DISCOVERY_DB   Notion database ID (required for Notion output)
  BOT_WALLETS           Comma-separated bot wallet addresses to analyze
  TOKEN_MINTS           Comma-separated token mint addresses to investigate
  MAX_LAG_SECONDS       Max seconds between lead buy and bot buy (default: 60)
  SEED_WINDOW_SECONDS   Time window to seed around bot's buy time (default: 300)
  MIN_CONFIDENCE        Minimum confidence to push to Notion (default: 0.55)
  DB_PATH               SQLite database file path (default: discovery.db)
  WALLETS_FILE          Wallet registry file (default: wallets.json)
  SKIP_NOTION           Set to "1" to skip Notion output (dry run)
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

# ── Load .env if present (local runs) ────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── Config from environment ───────────────────────────────────────────────────
HELIUS_API_KEY      = os.environ.get("HELIUS_API_KEY", "")
NOTION_TOKEN        = os.environ.get("NOTION_TOKEN", "")
NOTION_DISCOVERY_DB = os.environ.get("NOTION_DISCOVERY_DB", "")
BOT_WALLETS_CSV     = os.environ.get("BOT_WALLETS", "").strip()
TOKEN_MINTS_CSV     = os.environ.get("TOKEN_MINTS", "").strip()
MAX_LAG             = int(os.environ.get("MAX_LAG_SECONDS", "60"))
SEED_WINDOW         = int(os.environ.get("SEED_WINDOW_SECONDS", "300"))
MIN_CONFIDENCE      = float(os.environ.get("MIN_CONFIDENCE", "0.55"))
DB_PATH             = os.environ.get("DB_PATH", "discovery.db")
SKIP_NOTION         = os.environ.get("SKIP_NOTION", "0") == "1"

# Parse lists
BOT_WALLETS  = [w.strip() for w in BOT_WALLETS_CSV.split(",") if w.strip()] if BOT_WALLETS_CSV else []
TOKEN_MINTS  = [m.strip() for m in TOKEN_MINTS_CSV.split(",") if m.strip()] if TOKEN_MINTS_CSV else []


def _die(msg: str):
    print(f"ERROR: {msg}", flush=True)
    sys.exit(1)


def _banner(text: str):
    line = "─" * len(text)
    print(f"\n{line}\n{text}\n{line}", flush=True)


# ── Validate ──────────────────────────────────────────────────────────────────
if not HELIUS_API_KEY:
    _die("HELIUS_API_KEY is not set. Add it to your GitHub Secrets / .env file.")

if not BOT_WALLETS and not TOKEN_MINTS:
    _die(
        "Nothing to do. Set BOT_WALLETS and/or TOKEN_MINTS in your "
        "GitHub Secrets / .env file."
    )

use_notion = bool(NOTION_TOKEN and NOTION_DISCOVERY_DB and not SKIP_NOTION)
if not use_notion:
    if SKIP_NOTION:
        print("[notion] Skipping Notion output (SKIP_NOTION=1)")
    else:
        print("[notion] NOTION_TOKEN or NOTION_DISCOVERY_DB not set — results printed only")


# ── Imports (after env check so errors are readable) ─────────────────────────
from src.db import init_db, get_candidates
from src.wallets import WalletRegistry
from src.helius import HeliusClient
from src.bot_tracker import investigate_tokens, find_bot_leads


async def run_investigation() -> list[dict]:
    """
    Run the full investigation pipeline and return a flat list of candidate dicts
    ready to push to Notion.
    """
    init_db(DB_PATH)

    # Load wallet registry (may be empty if wallets.json doesn't exist)
    registry = WalletRegistry()
    client   = HeliusClient()

    all_candidates: list[dict] = []

    try:
        # ── 1. Investigate known tokens (main training workflow) ──────────────
        if TOKEN_MINTS and BOT_WALLETS:
            for bot_wallet in BOT_WALLETS:
                _banner(f"Investigating {len(TOKEN_MINTS)} token(s) vs bot {bot_wallet[:16]}…")
                try:
                    result = await investigate_tokens(
                        token_mints=TOKEN_MINTS,
                        client=client,
                        registry=registry,
                        bot_wallet=bot_wallet,
                        max_lag=MAX_LAG,
                        path=DB_PATH,
                    )

                    # Report seeding
                    for mint, n in (result.get("seeded") or {}).items():
                        print(f"  seeded {mint[:20]}… → {n} records")

                    # Report bot buy times
                    for mint, buy in result.get("bot_buys", {}).items():
                        import datetime as dt
                        ts = dt.datetime.utcfromtimestamp(buy.bot_block_time).strftime("%Y-%m-%d %H:%M:%S")
                        print(f"  bot bought {mint[:20]}…  at {ts} UTC")

                    # Intersection — strongest signal
                    intersection = result.get("intersection", [])
                    if intersection:
                        print(f"\n  ⚡ INTERSECTION ({len(intersection)} wallets in ALL tokens):")
                        for w in intersection:
                            winfo = registry.get(w)
                            label = f"  [{winfo.label}]" if winfo else ""
                            print(f"    {w}{label}")

                    # Collect ranked candidates
                    for r in result.get("ranked", []):
                        all_candidates.append({
                            "address":         r.wallet,
                            "match_type":      "bot_lead",
                            "confidence":      r.confidence,
                            "tokens_matched":  r.tokens_led,
                            "avg_lag_seconds": round(r.avg_lag_seconds, 1),
                            "known_as":        r.known_label or "",
                            "bot_wallet":      bot_wallet,
                        })

                except Exception as exc:
                    print(f"  ERROR in investigate: {exc}", flush=True)

        # ── 2. Bot-lead scan (accumulation mode — uses historical DB data) ────
        elif BOT_WALLETS and not TOKEN_MINTS:
            for bot_wallet in BOT_WALLETS:
                _banner(f"Bot-lead scan: {bot_wallet[:16]}…")
                try:
                    results = await find_bot_leads(
                        bot_wallet=bot_wallet,
                        client=client,
                        registry=registry,
                        lookback=100,
                        max_lag=MAX_LAG,
                        path=DB_PATH,
                    )
                    print(f"  {len(results)} lead wallets found")
                    for r in results:
                        all_candidates.append({
                            "address":         r.wallet,
                            "match_type":      "bot_lead",
                            "confidence":      r.confidence,
                            "tokens_matched":  r.tokens_led,
                            "avg_lag_seconds": round(r.avg_lag_seconds, 1),
                            "known_as":        r.known_label or "",
                            "bot_wallet":      bot_wallet,
                        })
                except Exception as exc:
                    print(f"  ERROR in bot-lead scan: {exc}", flush=True)

        # ── 3. Co-purchase + sell-cluster (from accumulated DB data) ──────────
        _banner("Co-purchase + sell-cluster scan (DB only, 0 credits)")
        try:
            from src.db import get_co_purchase_pairs, get_sell_cluster_pairs
            from src.matcher import co_purchase_pattern_scan, coordinated_sell_scan

            pairs = get_co_purchase_pairs(min_shared=3, path=DB_PATH)
            print(f"  co-purchase: {len(pairs)} pairs")
            for p in pairs:
                shared    = p["shared_tokens"]
                fee_hits  = p.get("fee_matches") or 0
                conf      = 0.40 + min((shared - 3) * 0.08, 0.48)
                if fee_hits:
                    conf = min(conf + 0.07, 0.95)
                conf = round(conf, 2)
                for addr, matched in [(p["addr1"], p["addr2"]), (p["addr2"], p["addr1"])]:
                    winfo = registry.get(addr)
                    all_candidates.append({
                        "address":        addr,
                        "match_type":     "co_purchase",
                        "confidence":     conf,
                        "tokens_matched": shared,
                        "known_as":       winfo.label if winfo else "",
                        "bot_wallet":     "",
                    })

            sell_pairs = get_sell_cluster_pairs(window_seconds=10, min_co_sells=1, path=DB_PATH)
            print(f"  sell-cluster: {len(sell_pairs)} pairs")
            for p in sell_pairs:
                co_sells = p["co_sell_count"]
                avg_dt   = p.get("avg_time_diff_seconds") or 10
                conf     = 0.70
                if avg_dt <= 10:
                    conf += 0.15
                elif avg_dt <= 30:
                    conf += 0.08
                if p.get("fee_matches"):
                    conf += 0.05
                conf = min(round(conf + max(co_sells - 1, 0) * 0.08, 2), 0.97)
                for addr in [p["addr1"], p["addr2"]]:
                    winfo = registry.get(addr)
                    all_candidates.append({
                        "address":        addr,
                        "match_type":     "coordinated_sell",
                        "confidence":     conf,
                        "tokens_matched": co_sells,
                        "known_as":       winfo.label if winfo else "",
                        "bot_wallet":     "",
                    })
        except Exception as exc:
            print(f"  ERROR in pattern scans: {exc}", flush=True)

    finally:
        await client.close()

    return all_candidates


def print_summary(candidates: list[dict]):
    if not candidates:
        print("\nNo candidates found.", flush=True)
        return
    high = [c for c in candidates if c["confidence"] >= 0.80]
    med  = [c for c in candidates if 0.55 <= c["confidence"] < 0.80]
    print(f"\n{'='*60}")
    print(f"SUMMARY: {len(candidates)} candidates total")
    print(f"  High confidence (≥80%): {len(high)}")
    print(f"  Medium confidence (55-79%): {len(med)}")
    print(f"{'='*60}")
    for c in sorted(candidates, key=lambda x: -x["confidence"])[:20]:
        label = f"  [{c['known_as']}]" if c.get("known_as") else ""
        lag   = f"  lag={c['avg_lag_seconds']:.0f}s" if c.get("avg_lag_seconds") else ""
        print(
            f"  {c['confidence']:.0%}  {c['address'][:16]}…"
            f"  {c['match_type']}{label}{lag}",
            flush=True,
        )


async def main():
    t0 = time.monotonic()

    print("=" * 60, flush=True)
    print("Solana Wallet Discovery — CI Run", flush=True)
    print(f"Bot wallets:  {len(BOT_WALLETS)}", flush=True)
    print(f"Token mints:  {len(TOKEN_MINTS)}", flush=True)
    print(f"Max lag:      {MAX_LAG}s", flush=True)
    print(f"Notion:       {'yes' if use_notion else 'no'}", flush=True)
    print("=" * 60, flush=True)

    candidates = await run_investigation()
    print_summary(candidates)

    # ── Push to Notion ────────────────────────────────────────────────────────
    if use_notion and candidates:
        from src.notion_writer import push_candidates
        _banner("Pushing to Notion…")
        stats = push_candidates(
            token=NOTION_TOKEN,
            db_id=NOTION_DISCOVERY_DB,
            candidates=candidates,
            min_confidence=MIN_CONFIDENCE,
            verbose=True,
        )
        print(
            f"\nNotion: {stats['created']} created, "
            f"{stats['updated']} updated, "
            f"{stats['skipped']} skipped, "
            f"{stats['errors']} errors",
            flush=True,
        )

    elapsed = time.monotonic() - t0
    print(f"\nDone in {elapsed:.0f}s", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
