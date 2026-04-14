#!/usr/bin/env python3
"""
find_funded_wallets.py
----------------------
Find every wallet that a given Solana address has funded or interacted with.

Algorithm
---------
1. Page through ALL transaction signatures for the source wallet via
   getSignaturesForAddress (max 1 000 per call, paginated).
2. Fetch each transaction in batches via getTransaction.
3. For every transaction, compare pre- vs post-SOL balances across all
   account keys to classify each peer wallet as:
     - FUNDED      : peer's balance increased AND source's balance decreased
                     (i.e. source sent SOL to this peer)
     - RECEIVED    : peer's balance decreased AND source's balance increased
                     (i.e. peer sent SOL to source)
     - INTERACTED  : appeared in the same transaction but no direct SOL flow
                     from/to source

Output
------
Prints a summary to stdout and writes results.csv in the same directory.

Usage
-----
  python find_funded_wallets.py [WALLET_ADDRESS] [--rpc URL] [--delay SECONDS]

Requirements: Python 3.8+ stdlib only (no pip installs needed).
"""

import sys
import json
import time
import random
import urllib.request
import urllib.error
import csv
import argparse
from datetime import datetime, timezone

# ── Defaults ──────────────────────────────────────────────────────────────────
DEFAULT_RPC   = "https://api.mainnet-beta.solana.com"
BATCH_SIZE    = 10        # transactions fetched per batch
BATCH_PAUSE   = 1.2       # seconds between batches (rate-limit courtesy)
MAX_RETRIES   = 5
BACKOFF_CAP   = 30.0
SIG_PAGE_SIZE = 1000      # max per getSignaturesForAddress call


# ── Helpers ───────────────────────────────────────────────────────────────────
def log(msg):
    print(msg, flush=True)


def mask(addr):
    return f"{addr[:4]}...{addr[-4:]}" if len(addr) >= 8 else addr


def backoff(attempt):
    d = min(2 ** attempt + random.uniform(0, 0.5), BACKOFF_CAP)
    log(f"  [retry] waiting {d:.1f}s...")
    time.sleep(d)


def http_post(url, payload, timeout=30):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", errors="replace") or "{}")


def rpc(url, payload, label=""):
    last_err = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = http_post(url, payload)
            # batch response is a list
            if isinstance(resp, list):
                return resp
            if resp.get("error"):
                raise Exception(f"RPC error: {resp['error']}")
            return resp
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode()
            except Exception:
                detail = ""
            last_err = f"HTTP {e.code}: {detail[:120]}"
            log(f"  [{label}] {last_err}")
            if e.code in {429, 500, 502, 503, 504}:
                backoff(attempt)
            else:
                raise
        except Exception as ex:
            last_err = str(ex)
            log(f"  [{label}] {last_err}")
            backoff(attempt)
    raise Exception(f"RPC call failed after {MAX_RETRIES} attempts: {last_err}")


# ── Step 1: collect all signatures ────────────────────────────────────────────
def get_all_signatures(rpc_url, wallet):
    log(f"\n[1/3] Fetching transaction signatures for {mask(wallet)} ...")
    all_sigs = []
    before = None
    page = 0
    while True:
        page += 1
        params = [wallet, {"limit": SIG_PAGE_SIZE, "commitment": "finalized"}]
        if before:
            params[1]["before"] = before
        resp = rpc(rpc_url, {
            "jsonrpc": "2.0", "id": 1,
            "method": "getSignaturesForAddress",
            "params": params,
        }, label="getSigs")
        results = resp.get("result", [])
        if not results:
            break
        all_sigs.extend(r["signature"] for r in results)
        log(f"  page {page}: +{len(results)} sigs  (total so far: {len(all_sigs)})")
        if len(results) < SIG_PAGE_SIZE:
            break
        before = results[-1]["signature"]
        time.sleep(0.5)   # be polite between signature pages

    log(f"  Total signatures: {len(all_sigs)}")
    return all_sigs


# ── Step 2: fetch transactions in batches ─────────────────────────────────────
def fetch_transactions(rpc_url, signatures, delay):
    log(f"\n[2/3] Fetching {len(signatures)} transactions (batch={BATCH_SIZE}) ...")
    txns = []
    total = len(signatures)
    for start in range(0, total, BATCH_SIZE):
        chunk = signatures[start:start + BATCH_SIZE]
        batch = [
            {
                "jsonrpc": "2.0", "id": i,
                "method": "getTransaction",
                "params": [sig, {"encoding": "json",
                                 "maxSupportedTransactionVersion": 0}],
            }
            for i, sig in enumerate(chunk)
        ]
        responses = rpc(rpc_url, batch, label="getTx")
        if not isinstance(responses, list):
            responses = [responses]
        for item in responses:
            result = item.get("result")
            if result:
                txns.append(result)
        done = min(start + BATCH_SIZE, total)
        log(f"  fetched {done}/{total} ({100*done//total}%)")
        if done < total:
            time.sleep(delay)
    log(f"  Transactions with data: {len(txns)}")
    return txns


# ── Step 3: parse wallet relationships ────────────────────────────────────────
def parse_relationships(txns, source_wallet):
    log(f"\n[3/3] Parsing relationships ...")

    # wallet -> {"funded", "received", "interacted", "tx_count", "sol_sent", "sol_received"}
    peers = {}

    def upsert(addr, role, sol_delta=0.0):
        if addr not in peers:
            peers[addr] = {
                "address": addr,
                "role": role,
                "tx_count": 0,
                "sol_sent": 0.0,
                "sol_received": 0.0,
            }
        # role priority: FUNDED > RECEIVED > INTERACTED
        priority = {"FUNDED": 3, "RECEIVED": 2, "INTERACTED": 1}
        if priority.get(role, 0) > priority.get(peers[addr]["role"], 0):
            peers[addr]["role"] = role
        peers[addr]["tx_count"] += 1
        if role == "FUNDED":
            peers[addr]["sol_received"] += sol_delta
        elif role == "RECEIVED":
            peers[addr]["sol_sent"] += sol_delta

    for txn in txns:
        try:
            meta = txn.get("meta", {}) or {}
            tx   = txn.get("transaction", {}) or {}
            msg  = tx.get("message", {}) or {}

            # account keys (static + loaded from ALT)
            static_keys = msg.get("accountKeys", [])
            loaded      = meta.get("loadedAddresses", {}) or {}
            all_keys    = (
                static_keys
                + loaded.get("writable", [])
                + loaded.get("readonly", [])
            )

            pre_bals  = meta.get("preBalances",  [])
            post_bals = meta.get("postBalances", [])

            # find source index
            try:
                src_idx = all_keys.index(source_wallet)
            except ValueError:
                continue  # source not in this tx (shouldn't happen but guard)

            src_delta = (
                post_bals[src_idx] - pre_bals[src_idx]
                if src_idx < len(pre_bals) and src_idx < len(post_bals)
                else 0
            )  # lamports

            for i, addr in enumerate(all_keys):
                if addr == source_wallet:
                    continue
                if i >= len(pre_bals) or i >= len(post_bals):
                    upsert(addr, "INTERACTED")
                    continue

                peer_delta = post_bals[i] - pre_bals[i]  # lamports

                if src_delta < 0 and peer_delta > 0:
                    # source lost SOL, peer gained — source funded peer
                    upsert(addr, "FUNDED", peer_delta / 1e9)
                elif src_delta > 0 and peer_delta < 0:
                    # peer lost SOL, source gained — peer funded source
                    upsert(addr, "RECEIVED", abs(peer_delta) / 1e9)
                else:
                    upsert(addr, "INTERACTED")

        except Exception as ex:
            log(f"  [warn] could not parse tx: {ex}")
            continue

    return peers


# ── Output ────────────────────────────────────────────────────────────────────
def print_results(source_wallet, peers):
    funded     = [p for p in peers.values() if p["role"] == "FUNDED"]
    received   = [p for p in peers.values() if p["role"] == "RECEIVED"]
    interacted = [p for p in peers.values() if p["role"] == "INTERACTED"]

    funded.sort(key=lambda x: -x["sol_received"])
    received.sort(key=lambda x: -x["sol_sent"])
    interacted.sort(key=lambda x: -x["tx_count"])

    print("\n" + "=" * 70)
    print(f"SOURCE WALLET : {source_wallet}")
    print(f"FUNDED        : {len(funded)} wallets (source sent SOL)")
    print(f"RECEIVED FROM : {len(received)} wallets (source received SOL)")
    print(f"INTERACTED    : {len(interacted)} wallets (shared tx, no direct SOL flow)")
    print("=" * 70)

    if funded:
        print(f"\n--- FUNDED ({len(funded)}) ---")
        for p in funded:
            print(f"  {p['address']}  SOL sent: {p['sol_received']:.6f}  txs: {p['tx_count']}")

    if received:
        print(f"\n--- RECEIVED FROM ({len(received)}) ---")
        for p in received:
            print(f"  {p['address']}  SOL received: {p['sol_sent']:.6f}  txs: {p['tx_count']}")

    if interacted:
        print(f"\n--- INTERACTED ONLY ({len(interacted)}) ---")
        for p in interacted:
            print(f"  {p['address']}  txs: {p['tx_count']}")

    print()
    return funded, received, interacted


def write_csv(path, source_wallet, peers):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["source_wallet", "peer_wallet", "role",
                    "tx_count", "sol_sent_by_source", "sol_received_by_source"])
        for p in sorted(peers.values(), key=lambda x: (x["role"], -x["tx_count"])):
            w.writerow([
                source_wallet,
                p["address"],
                p["role"],
                p["tx_count"],
                round(p["sol_received"], 9),   # SOL sent BY source TO this peer
                round(p["sol_sent"],     9),   # SOL received BY source FROM this peer
            ])
    log(f"CSV written → {path}")


# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Find wallets funded/interacted with by a Solana address."
    )
    parser.add_argument("wallet", nargs="?",
                        default="F5CLqaNWNoYPJHMAUD4Vz1aY41dydWHaju9veHN7wwD2",
                        help="Source wallet address")
    parser.add_argument("--rpc",   default=DEFAULT_RPC, help="Solana RPC endpoint")
    parser.add_argument("--delay", type=float, default=BATCH_PAUSE,
                        help="Seconds between transaction batch fetches")
    parser.add_argument("--out",   default="results.csv", help="Output CSV file")
    args = parser.parse_args()

    wallet = args.wallet.strip()
    log(f"Solana Wallet Analyser")
    log(f"Source  : {wallet}")
    log(f"RPC     : {args.rpc}")
    log(f"Started : {datetime.now(timezone.utc).isoformat()}")

    signatures = get_all_signatures(args.rpc, wallet)
    if not signatures:
        log("No transactions found for this wallet. Exiting.")
        sys.exit(0)

    txns = fetch_transactions(args.rpc, signatures, args.delay)
    peers = parse_relationships(txns, wallet)

    print_results(wallet, peers)
    write_csv(args.out, wallet, peers)


if __name__ == "__main__":
    main()
