"""
Email verification via Apify actor (michael.g/email-verifier-validator).

Verifies emails before uploading to Instantly to protect sender reputation.
Cost: ~$0.001/email ($1 per 1,000 emails).

Results:
  - "ok"         → deliverable, safe to send
  - "catch_all"  → server accepts all, risky but usable
  - "invalid"    → does not exist, do NOT send
  - "disposable" → temp email, do NOT send
  - "unknown"    → could not determine, skip or retry
"""
from __future__ import annotations

import logging
import time
from typing import Any

import config
from storage.leads_db import get_connection

logger = logging.getLogger(__name__)

ACTOR_ID = "michael.g/email-verifier-validator"

# Actor returns status: "good", "risky", "bad"
# We map these for our DB and upload logic
SAFE_STATUSES = {"good"}

# Risky = catch-all or SMTP unreachable — usable but lower priority
RISKY_STATUSES = {"risky"}

# Bad = invalid, disposable — do NOT send
BAD_STATUSES = {"bad"}


def verify_single(email: str) -> dict:
    """
    Verify a single email address via Apify.

    Returns: {"email": str, "status": str, "reason": str}
    """
    if not email or not config.APIFY_API_KEY:
        return {"email": email, "status": "unknown", "reason": "no_api_key"}

    try:
        from apify_client import ApifyClient
        client = ApifyClient(config.APIFY_API_KEY)

        run = client.actor(ACTOR_ID).call(
            run_input={"emails": [email]},
            timeout_secs=60,
        )

        items = client.dataset(run["defaultDatasetId"]).list_items().items
        if items:
            item = items[0]
            return {
                "email": email,
                "status": item.get("status", "unknown"),
                "reason": item.get("reason", ""),
            }
    except Exception as e:
        logger.debug(f"Verify failed for {email}: {e}")

    return {"email": email, "status": "unknown", "reason": "error"}


def verify_batch(emails: list[str], batch_size: int = 50) -> list[dict]:
    """
    Verify a batch of emails via Apify.

    Sends emails in chunks to avoid timeouts.
    Returns list of {"email": str, "status": str, "reason": str}.
    """
    if not emails or not config.APIFY_API_KEY:
        return []

    try:
        from apify_client import ApifyClient
    except ImportError:
        logger.error("apify_client not installed — pip install apify-client")
        return []

    client = ApifyClient(config.APIFY_API_KEY)
    all_results: list[dict] = []

    for i in range(0, len(emails), batch_size):
        chunk = emails[i : i + batch_size]
        logger.info(
            "Verifying emails %d-%d of %d...",
            i + 1, min(i + batch_size, len(emails)), len(emails),
        )

        try:
            run = client.actor(ACTOR_ID).call(
                run_input={"emails": chunk},
                timeout_secs=120,
            )

            items = client.dataset(run["defaultDatasetId"]).list_items().items

            # Map results back by email
            result_map = {}
            for item in items:
                addr = (item.get("email") or "").lower()
                result_map[addr] = {
                    "email": addr,
                    "status": item.get("status", "unknown"),  # good/risky/bad
                    "reason": item.get("reason", ""),
                    "score": item.get("score", 0),
                    "disposable": item.get("disposable", False),
                    "catch_all": item.get("catch_all", False),
                }

            # Ensure every email in the chunk has a result
            for addr in chunk:
                key = addr.lower()
                if key in result_map:
                    all_results.append(result_map[key])
                else:
                    all_results.append({
                        "email": addr,
                        "status": "unknown",
                        "reason": "not_in_response",
                    })

        except Exception as e:
            logger.error("Batch verify failed for chunk %d-%d: %s", i + 1, i + len(chunk), e)
            for addr in chunk:
                all_results.append({
                    "email": addr,
                    "status": "unknown",
                    "reason": "batch_error",
                })

        # Small pause between batches
        if i + batch_size < len(emails):
            time.sleep(1)

    good = sum(1 for r in all_results if r["status"] == "good")
    risky = sum(1 for r in all_results if r["status"] == "risky")
    bad = sum(1 for r in all_results if r["status"] == "bad")
    unknown = len(all_results) - good - risky - bad
    logger.info(
        "Verification complete: %d good, %d risky, %d bad, %d unknown",
        good, risky, bad, unknown,
    )

    return all_results


def verify_unverified_leads(limit: int = 500) -> dict:
    """
    Pull leads from DB that have emails but haven't been verified yet,
    verify them, and update the DB.

    Returns summary: {"verified": int, "ok": int, "catch_all": int, "invalid": int, "unknown": int}
    """
    conn = get_connection()
    rows = conn.execute(
        """SELECT id, email FROM leads
        WHERE email IS NOT NULL AND email != ''
        AND (email_verified IS NULL OR email_verified = '')
        LIMIT ?""",
        (limit,),
    ).fetchall()
    conn.close()

    if not rows:
        logger.info("No unverified leads found")
        return {"verified": 0, "ok": 0, "catch_all": 0, "invalid": 0, "unknown": 0}

    leads = [dict(r) for r in rows]
    emails = [l["email"] for l in leads]
    logger.info(f"Verifying {len(emails)} emails...")

    results = verify_batch(emails)

    # Build lookup: email → result
    result_map = {r["email"].lower(): r for r in results}

    # Update DB
    conn = get_connection()
    counts = {"good": 0, "risky": 0, "bad": 0, "unknown": 0}

    for lead in leads:
        result = result_map.get(lead["email"].lower(), {})
        status = result.get("status", "unknown")
        reason = result.get("reason", "")

        bucket = status if status in counts else "unknown"
        counts[bucket] += 1

        conn.execute(
            """UPDATE leads SET
                email_verified = ?,
                email_verify_status = ?
            WHERE id = ?""",
            (status, reason, lead["id"]),
        )

    conn.commit()
    conn.close()

    counts["verified"] = len(leads)
    logger.info(
        "Verified %d leads: %d good, %d risky, %d bad, %d unknown",
        counts["verified"], counts["good"], counts["risky"],
        counts["bad"], counts["unknown"],
    )
    return counts


def get_verified_leads_for_upload(
    limit: int | None = None,
    include_risky: bool = True,
) -> list[dict]:
    """
    Get leads that are verified and safe to upload to Instantly.

    Only returns leads with email_verified = 'good' (and optionally 'risky').
    """
    statuses = ["'good'"]
    if include_risky:
        statuses.append("'risky'")

    status_clause = ", ".join(statuses)
    query = f"""
        SELECT * FROM leads
        WHERE email IS NOT NULL AND email != ''
        AND email_verified IN ({status_clause})
        AND delivered_at IS NULL
        ORDER BY scraped_at DESC
    """
    if limit:
        query += f" LIMIT {limit}"

    conn = get_connection()
    rows = conn.execute(query).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )

    parser = argparse.ArgumentParser(description="Email verifier")
    parser.add_argument(
        "--limit", type=int, default=500,
        help="Max emails to verify (default: 500)",
    )
    parser.add_argument(
        "--email", type=str,
        help="Verify a single email address",
    )
    args = parser.parse_args()

    if args.email:
        result = verify_single(args.email)
        print(f"{result['email']} → {result['status']} ({result['reason']})")
    else:
        from storage.leads_db import init_db
        init_db()
        summary = verify_unverified_leads(limit=args.limit)
        print(f"\nVerification summary: {summary}")
