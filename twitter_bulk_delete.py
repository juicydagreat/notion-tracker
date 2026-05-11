#!/usr/bin/env python3
"""
Twitter Bulk Delete + Unfollow
──────────────────────────────
Just run:  python3 twitter_bulk_delete.py

You'll be asked for your username and password.
Everything else is automatic.
"""

import subprocess, sys, importlib

# ── Auto-install missing packages ────────────────────────────────────────────
def install(pkg, import_as=None):
    name = import_as or pkg
    try:
        importlib.import_module(name)
    except ImportError:
        print(f"Installing {pkg}…")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", pkg])

install("playwright")
install("requests")

# Install playwright browsers if needed
try:
    from playwright.sync_api import sync_playwright
except Exception:
    subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium", "--with-deps"])
    from playwright.sync_api import sync_playwright

import time, json, getpass, re, requests

# ── Rate limit constants ──────────────────────────────────────────────────────
DELETE_LIMIT        = 50      # Twitter allows 50 deletes per 15 min
DELETE_WINDOW       = 15 * 60
DELAY_BETWEEN       = 1.2    # seconds between each delete / unfollow
UNFOLLOW_DAILY_MAX  = 400     # Twitter's safe daily unfollow limit

# ── Step 1: log in via real browser, grab auth tokens ────────────────────────
def get_tokens(username: str, password) -> dict:
    print("""
To get your login token, follow these steps:

  1. Open Chrome and go to x.com — log in if needed
  2. Press F12 to open Developer Tools
  3. Click the "Application" tab at the top
  4. On the left, expand "Cookies" then click "https://x.com"
  5. Find the row named  auth_token  and copy its value
  6. Find the row named  ct0         and copy its value

""")
    auth_token = input("Paste your auth_token here: ").strip()
    ct0        = input("Paste your ct0 here:        ").strip()

    if not auth_token or not ct0:
        print("Both values are required. Please try again.")
        sys.exit(1)

    print("\nGot it — continuing...\n")
    return {"auth_token": auth_token, "ct0": ct0}


# ── Step 2: get the numeric user ID ──────────────────────────────────────────
def get_user_id(username: str, tokens: dict) -> str:
    url = "https://api.twitter.com/graphql/SAMkL5y_N9pmahSw8yy6gA/UserByScreenName"
    features = json.dumps({
        "hidden_profile_likes_enabled": True,
        "hidden_profile_subscriptions_enabled": True,
        "rweb_tipjar_consumption_enabled": True,
        "verified_phone_label_enabled": False,
        "subscriptions_verification_info_is_identity_verified_enabled": True,
        "subscriptions_verification_info_verified_since_enabled": True,
        "highlights_tweets_tab_ui_enabled": True,
        "responsive_web_twitter_article_notes_tab_enabled": False,
        "creator_subscriptions_tweet_preview_api_enabled": True,
        "responsive_web_graphql_exclude_directive_enabled": True,
        "responsive_web_graphql_timeline_navigation_enabled": True,
        "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
        "tweetypie_unmention_optimization_enabled": True,
        "responsive_web_edit_tweet_api_enabled": True,
        "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
        "view_counts_everywhere_api_enabled": True,
        "longform_notetweets_consumption_enabled": True,
        "responsive_web_twitter_article_tweet_consumption_enabled": False,
        "tweet_awards_web_tipping_enabled": False,
        "freedom_of_speech_not_reach_fetch_enabled": True,
        "standardized_nudges_misinfo": True,
        "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
        "rweb_video_timestamps_enabled": True,
        "longform_notetweets_rich_text_read_enabled": True,
        "longform_notetweets_inline_media_enabled": True,
        "responsive_web_enhance_cards_enabled": False,
    })
    params = {
        "variables": json.dumps({"screen_name": username.lstrip("@"), "withSafetyModeUserFields": True}),
        "features":  features,
    }
    headers = _headers(tokens)
    r = requests.get(url, params=params, headers=headers, timeout=30)
    r.raise_for_status()
    return r.json()["data"]["user"]["result"]["rest_id"]


# ── Step 3: fetch all tweet IDs ───────────────────────────────────────────────
def fetch_all_ids(user_id: str, tokens: dict) -> list[str]:
    print("Fetching your tweets…")
    ids = []
    cursor = None
    url = "https://api.twitter.com/graphql/V7H0Ap3_Hh2FyS75OCDO3Q/UserTweets"

    features = json.dumps({
        "rweb_tipjar_consumption_enabled": True,
        "responsive_web_graphql_exclude_directive_enabled": True,
        "verified_phone_label_enabled": False,
        "creator_subscriptions_tweet_preview_api_enabled": True,
        "responsive_web_graphql_timeline_navigation_enabled": True,
        "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
        "communities_web_enable_tweet_community_results_fetch": True,
        "c9s_tweet_anatomy_moderator_badge_enabled": True,
        "articles_preview_enabled": True,
        "tweetypie_unmention_optimization_enabled": True,
        "responsive_web_edit_tweet_api_enabled": True,
        "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
        "view_counts_everywhere_api_enabled": True,
        "longform_notetweets_consumption_enabled": True,
        "responsive_web_twitter_article_tweet_consumption_enabled": True,
        "tweet_awards_web_tipping_enabled": False,
        "creator_subscriptions_quote_tweet_preview_enabled": False,
        "freedom_of_speech_not_reach_fetch_enabled": True,
        "standardized_nudges_misinfo": True,
        "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
        "rweb_video_timestamps_enabled": True,
        "longform_notetweets_rich_text_read_enabled": True,
        "longform_notetweets_inline_media_enabled": True,
        "responsive_web_enhance_cards_enabled": False,
    })

    while True:
        variables = {"userId": user_id, "count": 200, "includePromotedContent": False}
        if cursor:
            variables["cursor"] = cursor

        params = {"variables": json.dumps(variables), "features": features}
        r = requests.get(url, params=params, headers=_headers(tokens), timeout=30)

        if r.status_code == 429:
            print("  Pausing for rate limit…")
            time.sleep(60)
            continue
        r.raise_for_status()

        body = r.json()
        entries = (
            body.get("data", {})
                .get("user", {})
                .get("result", {})
                .get("timeline_v2", {})
                .get("timeline", {})
                .get("instructions", [])
        )

        batch_ids = []
        next_cursor = None
        for instruction in entries:
            for entry in instruction.get("entries", []):
                entry_id = entry.get("entryId", "")
                content = entry.get("content", {})
                # tweet entry
                if "tweet-" in entry_id:
                    result = (
                        content.get("itemContent", {})
                               .get("tweet_results", {})
                               .get("result", {})
                    )
                    tid = result.get("rest_id") or result.get("tweet", {}).get("rest_id")
                    if tid:
                        batch_ids.append(tid)
                # cursor-bottom for pagination
                if content.get("cursorType") == "Bottom":
                    next_cursor = content.get("value")

        ids.extend(batch_ids)
        print(f"  Found {len(ids)} so far…", flush=True)

        if not batch_ids or not next_cursor:
            break
        cursor = next_cursor

    return ids


# ── Step 4: delete tweets ─────────────────────────────────────────────────────
def delete_all(tweet_ids: list[str], tokens: dict):
    total   = len(tweet_ids)
    deleted = 0
    failed  = 0
    batch   = 0
    window_start = time.time()

    print(f"\nDeleting {total} tweet(s). This may take a while — don't close the window.\n")

    for i, tid in enumerate(tweet_ids, 1):
        batch += 1
        if batch > DELETE_LIMIT:
            elapsed = time.time() - window_start
            wait    = DELETE_WINDOW - elapsed
            if wait > 0:
                mins = int(wait // 60) + 1
                print(f"\n  Pausing {mins} min to avoid rate limits…\n", flush=True)
                time.sleep(wait + 5)
            batch = 1
            window_start = time.time()

        url  = "https://api.twitter.com/graphql/VaenaVgh5q5ih7kvyVjgtg/DeleteTweet"
        body = {
            "variables":       {"tweet_id": tid, "dark_request": False},
            "queryId":         "VaenaVgh5q5ih7kvyVjgtg",
        }
        headers = {**_headers(tokens), "Content-Type": "application/json"}
        try:
            r = requests.post(url, json=body, headers=headers, timeout=30)
            if r.status_code == 429:
                print("  Rate limited — pausing 16 min…", flush=True)
                time.sleep(16 * 60)
                r = requests.post(url, json=body, headers=headers, timeout=30)

            if r.ok:
                deleted += 1
                pct = int(deleted / total * 100)
                print(f"  [{pct}%] Deleted {deleted}/{total}", flush=True)
            else:
                failed += 1
                print(f"  FAILED tweet {tid}: {r.status_code}", flush=True)
        except Exception as e:
            failed += 1
            print(f"  ERROR tweet {tid}: {e}", flush=True)

        time.sleep(DELAY_BETWEEN)

    print(f"\nAll done!  Deleted: {deleted}  Failed: {failed}  Total: {total}")


# ── Step 5: fetch all following IDs ──────────────────────────────────────────
def fetch_following(user_id: str, tokens: dict) -> list[str]:
    print("Fetching accounts you follow…")
    ids = []
    cursor = -1

    while True:
        url = "https://api.twitter.com/1.1/friends/ids.json"
        params = {"user_id": user_id, "count": 5000, "stringify_ids": "true", "cursor": cursor}
        r = requests.get(url, params=params, headers=_headers(tokens), timeout=30)

        if r.status_code == 429:
            print("  Pausing for rate limit…")
            time.sleep(60)
            continue
        r.raise_for_status()

        body = r.json()
        batch = body.get("ids", [])
        ids.extend(batch)
        print(f"  Found {len(ids)} so far…", flush=True)

        cursor = body.get("next_cursor", 0)
        if not cursor:
            break

    return ids


# ── Step 6: unfollow everyone ─────────────────────────────────────────────────
def unfollow_all(user_ids: list[str], tokens: dict):
    total     = len(user_ids)
    unfollowed = 0
    failed    = 0

    print(f"\nUnfollowing {total} account(s). Don't close the window.\n")

    # Twitter's safe limit is ~400/day; we pace to ~1 every 3.5s to stay well under
    delay = max(DELAY_BETWEEN, 86400 / UNFOLLOW_DAILY_MAX)

    for i, uid in enumerate(user_ids, 1):
        url  = "https://api.twitter.com/1.1/friendships/destroy.json"
        headers = {**_headers(tokens), "Content-Type": "application/x-www-form-urlencoded"}
        try:
            r = requests.post(url, data={"user_id": uid}, headers=headers, timeout=30)

            if r.status_code == 429:
                print("  Rate limited — pausing 16 min…", flush=True)
                time.sleep(16 * 60)
                r = requests.post(url, data={"user_id": uid}, headers=headers, timeout=30)

            if r.ok:
                unfollowed += 1
                pct = int(unfollowed / total * 100)
                print(f"  [{pct}%] Unfollowed {unfollowed}/{total}", flush=True)
            else:
                failed += 1
                print(f"  FAILED uid {uid}: {r.status_code}", flush=True)
        except Exception as e:
            failed += 1
            print(f"  ERROR uid {uid}: {e}", flush=True)

        time.sleep(delay)

    print(f"\nDone unfollowing!  Unfollowed: {unfollowed}  Failed: {failed}  Total: {total}")


# ── Shared request headers ────────────────────────────────────────────────────
def _headers(tokens: dict) -> dict:
    return {
        "authorization":   "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA",
        "x-csrf-token":    tokens["ct0"],
        "cookie":          f"auth_token={tokens['auth_token']}; ct0={tokens['ct0']}",
        "x-twitter-auth-type":             "OAuth2Session",
        "x-twitter-client-language":       "en",
        "x-twitter-active-user":           "yes",
        "content-type":    "application/json",
    }


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print("=" * 50)
    print("   Twitter Bulk Delete + Unfollow")
    print("=" * 50)
    print()
    username = input("Twitter username (without @): ").strip()
    print()

    tokens  = get_tokens(username, None)
    user_id = get_user_id(username, tokens)

    # ── Tweets ────────────────────────────────────────────────────────────────
    tweet_ids = fetch_all_ids(user_id, tokens)
    print(f"\nFound {len(tweet_ids)} tweet(s).")

    do_tweets = False
    if tweet_ids:
        ans = input(f"Permanently delete all {len(tweet_ids)} tweets? Type YES to confirm: ").strip()
        do_tweets = (ans == "YES")
        if not do_tweets:
            print("Skipping tweet deletion.")

    # ── Following ─────────────────────────────────────────────────────────────
    following_ids = fetch_following(user_id, tokens)
    print(f"\nFound {len(following_ids)} account(s) you follow.")

    do_unfollow = False
    if following_ids:
        ans = input(f"Unfollow all {len(following_ids)} accounts? Type YES to confirm: ").strip()
        do_unfollow = (ans == "YES")
        if not do_unfollow:
            print("Skipping unfollow.")

    # ── Run ───────────────────────────────────────────────────────────────────
    if not do_tweets and not do_unfollow:
        print("\nNothing to do — exiting.")
        return

    if do_tweets:
        delete_all(tweet_ids, tokens)

    if do_unfollow:
        unfollow_all(following_ids, tokens)


if __name__ == "__main__":
    main()
