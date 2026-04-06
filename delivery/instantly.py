"""
Instantly.ai API v2 integration — upload enriched leads directly to campaigns.

Supports:
  - list_campaigns()              list all campaigns
  - create_campaign(name)         create a new campaign, returns campaign_id
  - upload_leads(campaign_id, leads)          upload a list of lead dicts
  - upload_leads_from_db(campaign_id, ...)    pull from SQLite and upload
"""
from __future__ import annotations

import logging
import time
from typing import Any

import requests

import config
from storage.leads_db import get_undelivered_leads, mark_delivered, get_leads_by_niche

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URL = "https://api.instantly.ai/api/v2"

# Instantly recommends batches of up to 1 000 leads per request
BATCH_SIZE = 100

# Seconds to wait after hitting a 429 before retrying
RATE_LIMIT_BACKOFF = 60

# Maximum upload retries per batch on transient errors
MAX_RETRIES = 3


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _headers() -> dict[str, str]:
    """Build the Authorization header from config."""
    api_key = config.INSTANTLY_API_KEY
    if not api_key:
        raise ValueError(
            "INSTANTLY_API_KEY is not set. Add it to your .env file."
        )
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def _request(
    method: str,
    path: str,
    *,
    json: Any = None,
    params: dict | None = None,
    retries: int = MAX_RETRIES,
) -> dict:
    """
    Make an authenticated request to the Instantly API v2.

    Handles:
      - 429 rate-limit: backs off and retries
      - 5xx transient errors: retries with exponential back-off
      - All other errors: raises immediately with a clear message
    """
    url = f"{BASE_URL}{path}"
    attempt = 0

    while attempt <= retries:
        try:
            response = requests.request(
                method,
                url,
                headers=_headers(),
                json=json,
                params=params,
                timeout=30,
            )
        except requests.RequestException as exc:
            logger.error("Network error calling %s %s: %s", method, url, exc)
            raise

        if response.status_code == 429:
            retry_after = int(response.headers.get("Retry-After", RATE_LIMIT_BACKOFF))
            logger.warning(
                "Rate limited by Instantly API. Waiting %s seconds before retry...",
                retry_after,
            )
            time.sleep(retry_after)
            attempt += 1
            continue

        if response.status_code >= 500:
            wait = 2 ** attempt
            logger.warning(
                "Server error %s from %s %s. Retrying in %ss (attempt %s/%s)...",
                response.status_code, method, url, wait, attempt + 1, retries,
            )
            time.sleep(wait)
            attempt += 1
            continue

        # All other non-2xx responses are raised immediately
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            logger.error(
                "HTTP %s from %s %s: %s",
                response.status_code, method, url, response.text,
            )
            raise

        # Try to parse JSON; fall back to empty dict for 204 No Content etc.
        try:
            return response.json()
        except ValueError:
            return {}

    raise RuntimeError(
        f"Instantly API {method} {url} failed after {retries} retries."
    )


# ---------------------------------------------------------------------------
# Name splitting utility
# ---------------------------------------------------------------------------

def _split_name(full_name: str | None) -> tuple[str, str]:
    """
    Split a full name string into (first_name, last_name).

    Examples:
      "John Smith"       -> ("John", "Smith")
      "Mary Jane Watson" -> ("Mary", "Jane Watson")
      "Madonna"          -> ("Madonna", "")
      None / ""          -> ("", "")
    """
    if not full_name:
        return ("", "")
    parts = full_name.strip().split()
    first = parts[0] if parts else ""
    last = " ".join(parts[1:]) if len(parts) > 1 else ""
    return (first, last)


# ---------------------------------------------------------------------------
# Lead formatting
# ---------------------------------------------------------------------------

def _format_lead(lead: dict) -> dict:
    """
    Convert an internal lead dict (DB row) into the Instantly API lead payload.

    Instantly v2 /leads schema:
      email          (required)
      first_name
      last_name
      company_name
      custom_variables  dict of arbitrary key/value pairs (used in templates)
    """
    first_name, last_name = _split_name(lead.get("decision_maker_name"))

    custom_variables: dict[str, Any] = {}

    # Outreach personalisation fields
    if lead.get("outreach_angle"):
        custom_variables["outreach_angle"] = lead["outreach_angle"]
    if lead.get("angle_type"):
        custom_variables["angle_type"] = lead["angle_type"]

    # Business context fields
    for field in ("city", "state", "niche", "website", "phone"):
        value = lead.get(field)
        if value is not None:
            custom_variables[field] = str(value)

    # Numeric / boolean fields — coerce to strings for template compatibility
    if lead.get("rating") is not None:
        custom_variables["rating"] = str(lead["rating"])
    if lead.get("review_count") is not None:
        custom_variables["review_count"] = str(lead["review_count"])

    has_ads = lead.get("has_facebook_ads")
    if has_ads is not None:
        custom_variables["has_facebook_ads"] = "yes" if has_ads else "no"

    payload: dict[str, Any] = {
        "email": lead.get("email", ""),
        "first_name": first_name,
        "last_name": last_name,
        "company_name": lead.get("name", ""),
    }

    if custom_variables:
        payload["custom_variables"] = custom_variables

    return payload


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def list_campaigns() -> list[dict]:
    """
    Return all campaigns in the workspace.

    Each item in the returned list contains:
      id     (str)  — campaign UUID
      name   (str)
      status (str)  — e.g. "active", "paused", "completed"
    """
    result = _request("GET", "/campaigns")

    # The v2 response may come back as {"data": [...]} or as a bare list
    items: list[dict] = []
    if isinstance(result, list):
        items = result
    elif isinstance(result, dict):
        items = result.get("data", result.get("campaigns", result.get("items", [])))

    campaigns = []
    for item in items:
        campaigns.append(
            {
                "id": item.get("id") or item.get("campaign_id") or item.get("uuid"),
                "name": item.get("name"),
                "status": item.get("status"),
            }
        )

    logger.info("Found %d campaigns", len(campaigns))
    return campaigns


def create_campaign(name: str) -> str:
    """
    Create a new Instantly campaign.

    Returns the campaign_id (UUID string) of the newly created campaign.
    """
    if not name or not name.strip():
        raise ValueError("Campaign name must not be empty.")

    payload = {"name": name.strip()}
    result = _request("POST", "/campaigns", json=payload)

    # Response may be {"id": "..."} or {"campaign_id": "..."} or {"data": {...}}
    campaign_id = (
        result.get("id")
        or result.get("campaign_id")
        or result.get("uuid")
        or (result.get("data") or {}).get("id")
    )

    if not campaign_id:
        raise RuntimeError(
            f"Could not extract campaign_id from Instantly response: {result}"
        )

    logger.info("Created campaign '%s' with id=%s", name, campaign_id)
    return campaign_id


def upload_leads(
    campaign_id: str,
    leads: list[dict],
    *,
    mark_as_delivered: bool = False,
) -> dict:
    """
    Upload a list of lead dicts to the given Instantly campaign.

    Args:
        campaign_id:        Target campaign UUID.
        leads:              List of internal lead dicts (DB rows or equivalent).
        mark_as_delivered:  If True, call mark_delivered() for successfully
                            uploaded leads that have a numeric 'id' field.

    Returns a summary dict:
        {
            "total":    int,   # leads attempted
            "uploaded": int,   # leads accepted by Instantly
            "skipped":  int,   # leads without a valid email (skipped locally)
            "failed":   int,   # leads that caused API errors
        }
    """
    if not campaign_id:
        raise ValueError("campaign_id must not be empty.")

    valid_leads = [l for l in leads if l.get("email", "").strip()]
    skipped = len(leads) - len(valid_leads)

    if skipped:
        logger.warning(
            "Skipping %d leads with missing email (out of %d total)",
            skipped, len(leads),
        )

    uploaded = 0
    failed = 0
    delivered_ids: list[int] = []

    # Chunk into batches to stay within Instantly's recommended limits
    for batch_start in range(0, len(valid_leads), BATCH_SIZE):
        batch = valid_leads[batch_start : batch_start + BATCH_SIZE]
        formatted = [_format_lead(lead) for lead in batch]

        payload = {
            "campaign_id": campaign_id,
            "leads": formatted,
        }

        try:
            result = _request("POST", "/leads/add", json=payload)
            batch_uploaded = len(formatted)

            # Some API versions return a count we can cross-check
            if isinstance(result, dict):
                count = result.get("uploaded") or result.get("count") or result.get("added")
                if count is not None:
                    batch_uploaded = int(count)

            uploaded += batch_uploaded
            logger.info(
                "Uploaded batch %d-%d (%d leads) to campaign %s",
                batch_start + 1,
                batch_start + len(batch),
                batch_uploaded,
                campaign_id,
            )

            if mark_as_delivered:
                for lead in batch:
                    lead_id = lead.get("id")
                    if isinstance(lead_id, int):
                        delivered_ids.append(lead_id)

        except (requests.HTTPError, RuntimeError) as exc:
            logger.error(
                "Failed to upload batch %d-%d: %s",
                batch_start + 1,
                batch_start + len(batch),
                exc,
            )
            failed += len(batch)

    if mark_as_delivered and delivered_ids:
        mark_delivered(delivered_ids)
        logger.info("Marked %d leads as delivered in DB", len(delivered_ids))

    summary = {
        "total": len(leads),
        "uploaded": uploaded,
        "skipped": skipped,
        "failed": failed,
    }
    logger.info(
        "Upload complete — total=%d uploaded=%d skipped=%d failed=%d",
        summary["total"], summary["uploaded"], summary["skipped"], summary["failed"],
    )
    return summary


def upload_leads_from_db(
    campaign_id: str,
    *,
    niche: str | None = None,
    limit: int | None = None,
    mark_as_delivered: bool = True,
) -> dict:
    """
    Pull undelivered leads from the SQLite database and upload them to Instantly.

    Args:
        campaign_id:        Target campaign UUID.
        niche:              If provided, only upload leads for this niche label
                            (e.g. "roofing", "hvac").  Niche leads are fetched
                            with get_leads_by_niche() which includes already-
                            delivered leads; pass mark_as_delivered=False if
                            you are using this purely for re-export.
                            If None, fetches all undelivered leads via
                            get_undelivered_leads().
        limit:              Maximum number of leads to upload in this run.
        mark_as_delivered:  Whether to stamp delivered_at in the DB after a
                            successful upload.  Defaults to True.

    Returns the same summary dict as upload_leads().
    """
    if niche:
        leads = get_leads_by_niche(niche)
        logger.info(
            "Fetched %d leads for niche '%s' from DB", len(leads), niche
        )
    else:
        leads = get_undelivered_leads(limit=limit)
        logger.info("Fetched %d undelivered leads from DB", len(leads))

    if limit and len(leads) > limit:
        leads = leads[:limit]
        logger.info("Capped to %d leads (limit=%d)", len(leads), limit)

    if not leads:
        logger.info("No leads to upload.")
        return {"total": 0, "uploaded": 0, "skipped": 0, "failed": 0}

    return upload_leads(
        campaign_id,
        leads,
        mark_as_delivered=mark_as_delivered,
    )


# ---------------------------------------------------------------------------
# CLI convenience entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="Instantly.ai lead uploader",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # list-campaigns
    sub.add_parser("list-campaigns", help="List all Instantly campaigns")

    # create-campaign
    p_create = sub.add_parser("create-campaign", help="Create a new campaign")
    p_create.add_argument("name", help="Campaign name")

    # upload
    p_upload = sub.add_parser(
        "upload", help="Upload DB leads to a campaign"
    )
    p_upload.add_argument("campaign_id", help="Instantly campaign UUID")
    p_upload.add_argument("--niche", help="Filter by niche label (e.g. roofing)")
    p_upload.add_argument(
        "--limit", type=int, help="Max number of leads to upload"
    )
    p_upload.add_argument(
        "--no-mark-delivered",
        action="store_true",
        help="Do not mark leads as delivered in DB after upload",
    )

    args = parser.parse_args()

    if args.command == "list-campaigns":
        campaigns = list_campaigns()
        if not campaigns:
            print("No campaigns found.")
        for c in campaigns:
            print(f"  [{c['status']}]  {c['name']}  ({c['id']})")

    elif args.command == "create-campaign":
        cid = create_campaign(args.name)
        print(f"Created campaign: {cid}")

    elif args.command == "upload":
        result = upload_leads_from_db(
            args.campaign_id,
            niche=args.niche,
            limit=args.limit,
            mark_as_delivered=not args.no_mark_delivered,
        )
        print(
            f"Done — uploaded={result['uploaded']}  "
            f"skipped={result['skipped']}  failed={result['failed']}"
        )
        if result["failed"]:
            sys.exit(1)
