"""
Notion API writer — pushes discovered wallet candidates into a Notion database.

Uses pure stdlib (urllib) so it works in GitHub Actions without pip install
for this module specifically.

Notion Database Schema
----------------------
Create a database at notion.so with these EXACT property names and types:

  Wallet           title        Short label (first 8 chars of address)
  Address          rich_text    Full wallet address
  Confidence       number       Format: Percent
  Match Type       select       bot_lead | co_purchase | coordinated_sell | same_block_fee
  Tokens Matched   number
  Avg Lag Seconds  number       (only relevant for bot_lead)
  Known As         rich_text    Label if already in your tracked list
  Bot Wallet       rich_text    Which bot wallet this was found via
  Status           select       New | Reviewing | Confirmed | False Positive
  Detected         date         When this was first found

Then share the database with your Notion integration and copy the DB ID from
the URL (the 32-char string after the last slash, before the ?).

Environment variables:
  NOTION_TOKEN          Your Notion integration secret (starts with secret_...)
  NOTION_DISCOVERY_DB   The database ID (32 hex chars, no dashes needed)
"""
import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Optional

NOTION_VERSION = "2022-06-28"
NOTION_API     = "https://api.notion.com/v1"

_RETRY_CODES = {429, 500, 502, 503, 504}


# ── Low-level HTTP ────────────────────────────────────────────────────────────

def _notion_req(
    token: str,
    path: str,
    body: dict,
    method: str = "POST",
    retries: int = 4,
) -> dict:
    url = f"{NOTION_API}{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Notion-Version": NOTION_VERSION,
    }
    last_err: Exception | None = None
    for attempt in range(retries):
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode(),
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read().decode("utf-8", errors="replace") or "{}")
                if isinstance(data, dict) and data.get("object") == "error":
                    raise RuntimeError(f"Notion error: {data.get('message', data)}")
                return data
        except urllib.error.HTTPError as e:
            body_text = ""
            try:
                body_text = e.read().decode()
            except Exception:
                pass
            last_err = RuntimeError(f"Notion HTTP {e.code}: {body_text[:200]}")
            if e.code in _RETRY_CODES:
                wait = min(2 ** attempt + 1, 30)
                time.sleep(wait)
            else:
                raise last_err
        except Exception as exc:
            last_err = exc
            time.sleep(min(2 ** attempt + 1, 15))
    raise last_err  # type: ignore


def _query_db(token: str, db_id: str, filter_body: dict) -> list[dict]:
    """Paginated database query."""
    results = []
    cursor: Optional[str] = None
    while True:
        body = {**filter_body, "page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        resp = _notion_req(token, f"/databases/{db_id}/query", body)
        results.extend(resp.get("results", []))
        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")
    return results


# ── Candidate upsert ─────────────────────────────────────────────────────────

def _build_properties(candidate: dict) -> dict:
    """Convert a candidate dict to Notion property format."""
    address = candidate.get("address", "")
    short   = address[:8] + "…" if len(address) > 8 else address
    conf    = float(candidate.get("confidence", 0))
    mtype   = candidate.get("match_type", "unknown")
    matched = int(candidate.get("tokens_matched", 0))
    avg_lag = candidate.get("avg_lag_seconds")
    known   = candidate.get("known_as", "") or ""
    bot     = candidate.get("bot_wallet", "") or ""
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    props: dict = {
        "Wallet": {
            "title": [{"text": {"content": short}}]
        },
        "Address": {
            "rich_text": [{"text": {"content": address}}]
        },
        "Confidence": {
            "number": round(conf, 4)
        },
        "Match Type": {
            "select": {"name": mtype}
        },
        "Tokens Matched": {
            "number": matched
        },
        "Known As": {
            "rich_text": [{"text": {"content": known[:200]}}]
        },
        "Status": {
            "select": {"name": "New"}
        },
        "Detected": {
            "date": {"start": now_iso}
        },
    }

    if avg_lag is not None:
        props["Avg Lag Seconds"] = {"number": round(float(avg_lag), 1)}

    if bot:
        props["Bot Wallet"] = {"rich_text": [{"text": {"content": bot[:100]}}]}

    return props


def _find_existing_page(
    token: str,
    db_id: str,
    address: str,
    match_type: str,
) -> Optional[str]:
    """
    Return the page_id if (address, match_type) already exists in the DB.
    We filter by Address rich_text and Match Type select.
    """
    try:
        pages = _query_db(token, db_id, {
            "filter": {
                "and": [
                    {
                        "property": "Address",
                        "rich_text": {"equals": address},
                    },
                    {
                        "property": "Match Type",
                        "select": {"equals": match_type},
                    },
                ]
            }
        })
        if pages:
            return pages[0]["id"]
    except Exception:
        pass
    return None


def upsert_candidate(
    token: str,
    db_id: str,
    candidate: dict,
    update_existing: bool = True,
) -> tuple[str, str]:
    """
    Create or update a Notion page for a discovered wallet candidate.

    Args:
        token:            Notion integration secret.
        db_id:            Target database ID.
        candidate:        Dict with keys: address, match_type, confidence,
                          tokens_matched, avg_lag_seconds, known_as, bot_wallet.
        update_existing:  If True, update confidence + lag on existing pages.
                          If False, skip pages that already exist.

    Returns:
        (page_id, action)  where action is "created" or "updated" or "skipped".
    """
    address    = candidate.get("address", "")
    match_type = candidate.get("match_type", "unknown")
    props      = _build_properties(candidate)

    existing_id = _find_existing_page(token, db_id, address, match_type)

    if existing_id:
        if not update_existing:
            return existing_id, "skipped"
        # Only update numeric fields + Known As (don't overwrite Status)
        update_props = {
            k: v for k, v in props.items()
            if k in ("Confidence", "Tokens Matched", "Avg Lag Seconds", "Known As", "Bot Wallet")
        }
        _notion_req(
            token,
            f"/pages/{existing_id}",
            {"properties": update_props},
            method="PATCH",
        )
        return existing_id, "updated"

    # Create new page
    resp = _notion_req(token, "/pages", {
        "parent": {"database_id": db_id},
        "properties": props,
    })
    return resp["id"], "created"


# ── Bulk writer ───────────────────────────────────────────────────────────────

def push_candidates(
    token: str,
    db_id: str,
    candidates: list[dict],
    min_confidence: float = 0.55,
    verbose: bool = True,
) -> dict:
    """
    Push a list of candidate dicts to Notion, skipping low-confidence ones.

    Each candidate dict should have:
      address, match_type, confidence, tokens_matched,
      avg_lag_seconds (optional), known_as (optional), bot_wallet (optional)

    Returns {"created": N, "updated": N, "skipped": N, "errors": N}
    """
    stats = {"created": 0, "updated": 0, "skipped": 0, "errors": 0}

    filtered = [c for c in candidates if float(c.get("confidence", 0)) >= min_confidence]
    if verbose:
        print(f"[notion] Pushing {len(filtered)} candidates (≥{min_confidence:.0%} confidence)…")

    for c in filtered:
        try:
            page_id, action = upsert_candidate(token, db_id, c)
            stats[action] += 1
            if verbose:
                addr = c.get("address", "?")[:12]
                print(f"  {action:8s}  {addr}…  {c.get('match_type')}  {c.get('confidence', 0):.0%}")
        except Exception as exc:
            stats["errors"] += 1
            if verbose:
                print(f"  ERROR     {c.get('address', '?')[:12]}…  {exc}")

    if verbose:
        print(
            f"[notion] Done — "
            f"{stats['created']} created, "
            f"{stats['updated']} updated, "
            f"{stats['skipped']} skipped, "
            f"{stats['errors']} errors"
        )
    return stats


def print_setup_guide():
    print("""
╔══════════════════════════════════════════════════════════════╗
║           Notion Discovery Database Setup Guide              ║
╚══════════════════════════════════════════════════════════════╝

1. Go to notion.so → New page → Table (full page)

2. Add these columns with EXACT names and types:

   Wallet           Title        (already exists — rename it)
   Address          Text
   Confidence       Number       → Format: Percent
   Match Type       Select       (values auto-created on first push)
   Tokens Matched   Number
   Avg Lag Seconds  Number
   Known As         Text
   Bot Wallet       Text
   Status           Select       (add: New, Reviewing, Confirmed, False Positive)
   Detected         Date

3. Share the database with your Notion integration:
   - Go to notion.so/my-integrations → New integration
   - Copy the "Internal Integration Secret" (starts with secret_...)
   - In your database: Share → search for your integration → Invite

4. Copy the Database ID from the URL:
   https://notion.so/your-workspace/XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX?v=...
                                    ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
                                    This 32-char string is your DB ID

5. Add to GitHub Secrets (Settings → Secrets → Actions):
   NOTION_TOKEN          secret_...your integration secret...
   NOTION_DISCOVERY_DB   the 32-char database ID

6. Add to .env for local use:
   NOTION_TOKEN=secret_...
   NOTION_DISCOVERY_DB=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
""")
