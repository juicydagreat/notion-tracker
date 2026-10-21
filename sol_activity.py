#!/usr/bin/env python3
"""
Activity checker — report which wallets had on-chain activity within a time window.
Wallet addresses are masked in output (AbCd...XyZ1) — safe for public repo logs.

Required env:
  WALLETS_CSV    — comma/space/newline-separated Solana pubkeys
  AFTER_AEST     — window start: "YYYY-MM-DD HH:MM"  (e.g. "2026-10-21 00:00")
  BEFORE_AEST    — window end:   "YYYY-MM-DD HH:MM"  (e.g. "2026-10-21 08:30")

Optional env:
  INDIVIDUAL_RPC — RPC endpoint (default: https://api.mainnet-beta.solana.com)
  INDIVIDUAL_DELAY, RPC_TIMEOUT, RPC_RETRIES, RPC_BACKOFF_CAP

Trigger via: Actions → Solana Notion Tracker → Run workflow → mode=activity
"""
import os, sys, json, time, random, re, calendar
import urllib.request, urllib.error
from datetime import datetime, timezone, timedelta

# ── Config ──────────────────────────────────────────────────────────────────
SOLANA_RPC_URL   = os.environ.get("INDIVIDUAL_RPC", "https://api.mainnet-beta.solana.com").strip()
WALLETS_CSV      = os.environ.get("WALLETS_CSV", "")
AFTER_AEST       = os.environ.get("AFTER_AEST",  "").strip()
BEFORE_AEST      = os.environ.get("BEFORE_AEST", "").strip()
RPC_TIMEOUT      = int(os.environ.get("RPC_TIMEOUT",      "30"))
RPC_RETRIES      = int(os.environ.get("RPC_RETRIES",      "5"))
RPC_BACKOFF_CAP  = float(os.environ.get("RPC_BACKOFF_CAP", "30"))
INDIVIDUAL_DELAY = float(os.environ.get("INDIVIDUAL_DELAY", "1.5"))
SIG_LIMIT        = int(os.environ.get("SIG_LIMIT", "1000"))

PUBKEY_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")


# ── Helpers ─────────────────────────────────────────────────────────────────
def fail(msg): print(f"ERROR: {msg}", flush=True); sys.exit(1)
def log(msg):  print(msg, flush=True)
def mask(addr): return f"{addr[:4]}...{addr[-4:]}" if len(addr) >= 8 else addr


def parse_wallets(raw):
    seen, out = set(), []
    for w in PUBKEY_RE.findall(raw or ""):
        if w not in seen:
            seen.add(w)
            out.append(w)
    return out


def backoff(attempt):
    d = min(2 ** attempt + random.uniform(0, 0.8), RPC_BACKOFF_CAP)
    log(f"  [backoff] {d:.1f}s")
    time.sleep(d)


def aest_offset(dt):
    """Return UTC offset in hours for a naive datetime treated as AEST/AEDT.
    AEDT (UTC+11): first Sun Oct → first Sun Apr.
    AEST (UTC+10): otherwise.
    Simple approximation: Oct–Mar = +11, Apr–Sep = +10.
    """
    month = dt.month
    return 11 if month >= 10 or month <= 3 else 10


def parse_aest(s):
    """Parse 'YYYY-MM-DD HH:MM' as AEST/AEDT and return UTC Unix timestamp (int)."""
    dt = datetime.strptime(s.strip(), "%Y-%m-%d %H:%M")
    offset_h = aest_offset(dt)
    utc_dt = dt - timedelta(hours=offset_h)
    return int(calendar.timegm(utc_dt.timetuple())), offset_h


def ts_to_aest(unix_ts, offset_h):
    utc_dt = datetime.fromtimestamp(unix_ts, tz=timezone.utc)
    local_dt = utc_dt + timedelta(hours=offset_h)
    return local_dt.strftime("%H:%M")


# ── RPC ─────────────────────────────────────────────────────────────────────
_RETRY_CODES = {408, 425, 429, 500, 502, 503, 504}


def rpc_call(payload):
    last_err = None
    for attempt in range(RPC_RETRIES):
        try:
            req = urllib.request.Request(
                SOLANA_RPC_URL,
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=RPC_TIMEOUT) as r:
                data = json.loads(r.read().decode("utf-8", errors="replace") or "{}")
            if isinstance(data, dict) and data.get("error"):
                raise Exception(f"RPC error: {data['error']}")
            return data
        except urllib.error.HTTPError as e:
            try:    detail = e.read().decode()
            except: detail = ""
            last_err = f"HTTP {e.code}: {detail}"
            if e.code in _RETRY_CODES:
                backoff(attempt)
            else:
                raise Exception(last_err)
        except Exception as ex:
            last_err = str(ex)
            backoff(attempt)
    raise Exception(f"RPC failed after {RPC_RETRIES} attempts: {last_err}")


def get_signatures(wallet, after_ts, before_ts):
    """Return list of (signature, blockTime) within [after_ts, before_ts)."""
    payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "getSignaturesForAddress",
        "params": [wallet, {"limit": SIG_LIMIT}],
    }
    resp = rpc_call(payload)
    sigs = resp.get("result") or []
    matches = []
    for item in sigs:
        bt = item.get("blockTime")
        if bt is None:
            continue
        if after_ts <= bt < before_ts:
            matches.append((item["signature"], bt))
    return matches


# ── Main ────────────────────────────────────────────────────────────────────
def main():
    if not AFTER_AEST or not BEFORE_AEST:
        fail("AFTER_AEST and BEFORE_AEST must both be set (format: 'YYYY-MM-DD HH:MM')")

    wallets = parse_wallets(WALLETS_CSV)
    if not wallets:
        fail("No valid Solana pubkeys found in WALLETS_CSV")

    after_ts,  offset_after  = parse_aest(AFTER_AEST)
    before_ts, offset_before = parse_aest(BEFORE_AEST)
    offset_h = offset_after  # use start-of-window offset for display

    if after_ts >= before_ts:
        fail("AFTER_AEST must be earlier than BEFORE_AEST")

    after_utc  = datetime.fromtimestamp(after_ts,  tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    before_utc = datetime.fromtimestamp(before_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    tz_label   = f"UTC+{offset_h}"

    log("=" * 60)
    log(f"Activity window:  {AFTER_AEST} → {BEFORE_AEST} AEST ({tz_label})")
    log(f"UTC window:       {after_utc} → {before_utc}")
    log(f"RPC:              {SOLANA_RPC_URL}")
    log(f"Wallets:          {len(wallets)}")
    log(f"Sig limit/wallet: {SIG_LIMIT}")
    log("=" * 60)

    active, inactive, errors = [], [], []

    for i, wallet in enumerate(wallets, 1):
        label = f"[{i:02d}/{len(wallets)}]  {mask(wallet)}"
        try:
            hits = get_signatures(wallet, after_ts, before_ts)
            if hits:
                times = sorted(h[1] for h in hits)
                earliest = ts_to_aest(times[0],  offset_h)
                latest   = ts_to_aest(times[-1], offset_h)
                log(f"  {label}  {len(hits):>3} txn(s)  "
                    f"earliest={earliest}  latest={latest} AEST")
                active.append((wallet, hits))
            else:
                log(f"  {label}  NO ACTIVITY")
                inactive.append(wallet)
        except Exception as ex:
            log(f"  {label}  ERROR: {ex}")
            errors.append((wallet, str(ex)))
        if i < len(wallets):
            time.sleep(INDIVIDUAL_DELAY)

    log("")
    log("=" * 60)
    log(f"ACTIVE:   {len(active)} wallet(s)")
    log(f"INACTIVE: {len(inactive)} wallet(s)")
    if errors:
        log(f"ERRORS:   {len(errors)} wallet(s)")
    log("=" * 60)

    if active:
        log("\nActive wallets detail:")
        for wallet, hits in active:
            times_aest = [ts_to_aest(bt, offset_h) for _, bt in sorted(hits, key=lambda x: x[1])]
            log(f"  {mask(wallet)}  [{', '.join(times_aest)}]")

    if errors:
        log("\nErrored wallets:")
        for wallet, err in errors:
            log(f"  {mask(wallet)}  {err}")


if __name__ == "__main__":
    main()
