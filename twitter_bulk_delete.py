#!/usr/bin/env python3
"""
Twitter Bulk Delete - deletes all tweets for an account using Twitter API v2.

Setup:
  1. Go to https://developer.twitter.com/en/portal/dashboard
  2. Create a Free tier app (read+write permissions)
  3. Generate OAuth 1.0a keys (API Key, API Secret, Access Token, Access Token Secret)
  4. Create a .env file or export the variables below:

  TWITTER_API_KEY=...
  TWITTER_API_SECRET=...
  TWITTER_ACCESS_TOKEN=...
  TWITTER_ACCESS_TOKEN_SECRET=...

Usage:
  python3 twitter_bulk_delete.py            # live delete
  python3 twitter_bulk_delete.py --dry-run  # preview only, no deletions
"""

import os
import sys
import time
import hmac
import hashlib
import base64
import urllib.parse
import secrets
import argparse
import json
import requests

# ── Rate-limit constants (Twitter API v2 free tier) ─────────────────────────
# DELETE /2/tweets/:id  → 50 requests / 15 min per user
# GET /2/users/:id/tweets → 10 requests / 15 min per user
FETCH_LIMIT    = 10          # max fetches before forced rest
FETCH_WINDOW   = 15 * 60    # 15 minutes in seconds
DELETE_LIMIT   = 50          # max deletes before forced rest
DELETE_WINDOW  = 15 * 60
DELETE_BATCH_SLEEP = 1.2    # polite delay between individual deletes (seconds)


def load_env():
    """Load credentials from .env file if present."""
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip())


def get_creds():
    load_env()
    keys = [
        "TWITTER_API_KEY",
        "TWITTER_API_SECRET",
        "TWITTER_ACCESS_TOKEN",
        "TWITTER_ACCESS_TOKEN_SECRET",
    ]
    creds = {k: os.environ.get(k) for k in keys}
    missing = [k for k, v in creds.items() if not v]
    if missing:
        print("Missing credentials:", ", ".join(missing))
        print(__doc__)
        sys.exit(1)
    return creds


# ── OAuth 1.0a signing ───────────────────────────────────────────────────────

def _pct(s: str) -> str:
    return urllib.parse.quote(str(s), safe="")


def oauth1_header(method: str, url: str, params: dict, creds: dict) -> str:
    oauth_params = {
        "oauth_consumer_key":     creds["TWITTER_API_KEY"],
        "oauth_nonce":            secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp":        str(int(time.time())),
        "oauth_token":            creds["TWITTER_ACCESS_TOKEN"],
        "oauth_version":          "1.0",
    }
    all_params = {**params, **oauth_params}
    sorted_params = "&".join(
        f"{_pct(k)}={_pct(v)}"
        for k, v in sorted(all_params.items())
    )
    base = "&".join([_pct(method.upper()), _pct(url), _pct(sorted_params)])
    signing_key = "&".join([
        _pct(creds["TWITTER_API_SECRET"]),
        _pct(creds["TWITTER_ACCESS_TOKEN_SECRET"]),
    ])
    sig = base64.b64encode(
        hmac.new(signing_key.encode(), base.encode(), hashlib.sha1).digest()
    ).decode()
    oauth_params["oauth_signature"] = sig
    header_parts = ", ".join(
        f'{_pct(k)}="{_pct(v)}"' for k, v in sorted(oauth_params.items())
    )
    return f"OAuth {header_parts}"


# ── API helpers ──────────────────────────────────────────────────────────────

def get_user_id(creds: dict) -> tuple[str, str]:
    """Return (user_id, username) for the authenticated user."""
    url = "https://api.twitter.com/2/users/me"
    headers = {"Authorization": oauth1_header("GET", url, {}, creds)}
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    data = r.json()["data"]
    return data["id"], data["username"]


def fetch_tweet_ids(user_id: str, creds: dict) -> list[str]:
    """
    Fetch all tweet IDs for the user, respecting the 10-req/15-min limit.
    Returns a flat list of tweet IDs (strings).
    """
    url = f"https://api.twitter.com/2/users/{user_id}/tweets"
    ids: list[str] = []
    pagination_token = None
    fetch_count = 0
    window_start = time.time()

    print("Fetching tweet IDs…", flush=True)
    while True:
        # enforce fetch rate limit
        fetch_count += 1
        if fetch_count > FETCH_LIMIT:
            elapsed = time.time() - window_start
            wait = FETCH_WINDOW - elapsed
            if wait > 0:
                print(f"  Fetch limit reached — sleeping {wait:.0f}s…", flush=True)
                time.sleep(wait)
            fetch_count = 1
            window_start = time.time()

        params = {"max_results": "100", "tweet.fields": "id"}
        if pagination_token:
            params["pagination_token"] = pagination_token

        headers = {"Authorization": oauth1_header("GET", url, params, creds)}
        r = requests.get(url, params=params, headers=headers, timeout=30)

        if r.status_code == 429:
            reset = int(r.headers.get("x-rate-limit-reset", time.time() + 60))
            wait = max(reset - time.time(), 1) + 5
            print(f"  Rate limited on fetch — sleeping {wait:.0f}s…", flush=True)
            time.sleep(wait)
            fetch_count = 0
            window_start = time.time()
            continue

        r.raise_for_status()
        body = r.json()
        batch = [t["id"] for t in body.get("data", [])]
        ids.extend(batch)
        print(f"  Fetched {len(ids)} tweet IDs so far…", flush=True)

        next_token = body.get("meta", {}).get("next_token")
        if not next_token:
            break
        pagination_token = next_token

    return ids


def delete_tweets(tweet_ids: list[str], creds: dict, dry_run: bool):
    """Delete tweets one by one, respecting the 50-req/15-min delete limit."""
    total = len(tweet_ids)
    if total == 0:
        print("No tweets to delete.")
        return

    action = "Would delete" if dry_run else "Deleting"
    print(f"\n{action} {total} tweet(s)…\n", flush=True)

    deleted = 0
    failed  = 0
    batch_count = 0
    window_start = time.time()

    for i, tweet_id in enumerate(tweet_ids, 1):
        # enforce delete rate limit
        batch_count += 1
        if batch_count > DELETE_LIMIT:
            elapsed = time.time() - window_start
            wait = DELETE_WINDOW - elapsed
            if wait > 0:
                print(f"\n  Delete limit reached — sleeping {wait:.0f}s…\n", flush=True)
                time.sleep(wait)
            batch_count = 1
            window_start = time.time()

        if dry_run:
            print(f"  [{i}/{total}] DRY RUN — tweet {tweet_id}")
            time.sleep(0.05)
            continue

        url = f"https://api.twitter.com/2/tweets/{tweet_id}"
        headers = {"Authorization": oauth1_header("DELETE", url, {}, creds)}

        try:
            r = requests.delete(url, headers=headers, timeout=30)

            if r.status_code == 429:
                reset = int(r.headers.get("x-rate-limit-reset", time.time() + 60))
                wait = max(reset - time.time(), 1) + 5
                print(f"\n  Rate limited on delete — sleeping {wait:.0f}s…\n", flush=True)
                time.sleep(wait)
                batch_count = 0
                window_start = time.time()
                # retry same tweet
                r = requests.delete(url, headers=headers, timeout=30)

            if r.status_code in (200, 204):
                deleted += 1
                print(f"  [{i}/{total}] Deleted {tweet_id}  (total deleted: {deleted})", flush=True)
            else:
                failed += 1
                print(f"  [{i}/{total}] FAILED  {tweet_id} — {r.status_code}: {r.text[:80]}", flush=True)

        except requests.RequestException as e:
            failed += 1
            print(f"  [{i}/{total}] ERROR   {tweet_id} — {e}", flush=True)

        time.sleep(DELETE_BATCH_SLEEP)

    print(f"\nDone. Deleted: {deleted}  Failed: {failed}  Total: {total}")


# ── Entry point ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Bulk-delete all your tweets.")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="List tweets that would be deleted without actually deleting them."
    )
    args = parser.parse_args()

    creds = get_creds()

    print("Authenticating…")
    user_id, username = get_user_id(creds)
    print(f"Logged in as @{username} (id: {user_id})\n")

    tweet_ids = fetch_tweet_ids(user_id, creds)
    print(f"\nFound {len(tweet_ids)} tweet(s) total.\n")

    if not tweet_ids:
        print("Nothing to do — your timeline is already empty.")
        return

    if not args.dry_run:
        confirm = input(
            f"About to permanently delete ALL {len(tweet_ids)} tweets for @{username}.\n"
            "Type YES to continue, anything else to abort: "
        ).strip()
        if confirm != "YES":
            print("Aborted.")
            return

    delete_tweets(tweet_ids, creds, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
