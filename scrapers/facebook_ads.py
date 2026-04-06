"""
Facebook Ad Library scraper using Apify.
Checks if businesses are running Facebook/Meta ads and extracts ad details.
"""
from __future__ import annotations

import json
import logging
import os
from urllib.parse import quote

from apify_client import ApifyClient
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

client = ApifyClient(os.getenv("APIFY_API_KEY"))
ACTOR_ID = "curious_coder/facebook-ads-library-scraper"

BATCH_SIZE = 20  # URLs per actor run


def _build_ad_library_url(business_name: str) -> str:
    """Build a Facebook Ad Library search URL for a business."""
    q = quote(business_name)
    return f"https://www.facebook.com/ads/library/?active_status=active&ad_type=all&country=US&q={q}"


def _name_similarity(name1: str, name2: str) -> float:
    """Simple word-overlap similarity between two names."""
    words1 = set(name1.lower().split())
    words2 = set(name2.lower().split())
    # Remove common filler words
    filler = {"the", "and", "of", "in", "llc", "inc", "corp", "co", "company"}
    words1 -= filler
    words2 -= filler
    if not words1 or not words2:
        return 0.0
    overlap = words1 & words2
    return len(overlap) / max(len(words1), len(words2))


def check_facebook_ads(leads: list[dict]) -> list[dict]:
    """
    Check Facebook Ad Library for each lead.
    Returns list of dicts with ad data per lead.
    """
    results = []

    for i in range(0, len(leads), BATCH_SIZE):
        batch = leads[i:i + BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1
        total_batches = (len(leads) - 1) // BATCH_SIZE + 1
        logger.info(f"FB Ads batch {batch_num}/{total_batches} ({len(batch)} leads)...")

        urls = []
        for lead in batch:
            name = lead.get("name", "").strip()
            if name:
                urls.append({"url": _build_ad_library_url(name)})

        if not urls:
            continue

        try:
            run = client.actor(ACTOR_ID).call(
                run_input={"urls": urls},
                timeout_secs=300,
            )
            items = client.dataset(run["defaultDatasetId"]).list_items().items
            logger.info(f"  Got {len(items)} raw ad results")

            # Match ads to leads by page name similarity
            for lead in batch:
                lead_name = lead.get("name", "").strip()
                lead_id = lead.get("id")

                # Find ads that match this business
                matching_ads = []
                for item in items:
                    snap = item.get("snapshot", {})
                    page_name = snap.get("page_name", "")
                    if _name_similarity(lead_name, page_name) >= 0.4:
                        matching_ads.append(item)

                ad_data = _build_ad_data(matching_ads)
                ad_data["lead_id"] = lead_id
                results.append(ad_data)

        except Exception as e:
            logger.warning(f"  FB Ads batch failed: {e}")
            for lead in batch:
                results.append({
                    "lead_id": lead.get("id"),
                    "has_ads": None,
                    "ad_count": 0,
                    "ad_samples": "[]",
                })

    return results


def check_single_business(business_name: str) -> dict:
    """Check Facebook ads for a single business. Useful for testing."""
    try:
        url = _build_ad_library_url(business_name)
        run = client.actor(ACTOR_ID).call(
            run_input={"urls": [{"url": url}]},
            timeout_secs=120,
        )
        items = client.dataset(run["defaultDatasetId"]).list_items().items

        # Filter to matching pages only
        matching = [
            item for item in items
            if _name_similarity(business_name, item.get("snapshot", {}).get("page_name", "")) >= 0.4
        ]

        result = _build_ad_data(matching)
        result["total_raw_results"] = len(items)
        result["matched_results"] = len(matching)

        # Also return unmatched pages for debugging
        all_pages = set()
        for item in items:
            all_pages.add(item.get("snapshot", {}).get("page_name", "unknown"))
        result["all_pages_found"] = list(all_pages)

        return result
    except Exception as e:
        logger.error(f"FB Ad check failed for {business_name}: {e}")
        return {"has_ads": None, "ad_count": 0, "error": str(e)}


def _build_ad_data(ads: list[dict]) -> dict:
    """Build structured ad data from a list of matched ads."""
    if not ads:
        return {
            "has_ads": False,
            "ad_count": 0,
            "ad_samples": "[]",
        }

    # Extract rich ad samples (up to 5) with text, link, title, dates
    samples = []
    for ad in ads[:5]:
        snap = ad.get("snapshot", {})
        body = snap.get("body", {})
        text = body.get("text", "") if isinstance(body, dict) else str(body or "")
        link_url = snap.get("link_url", "") or ""
        link_caption = snap.get("link_caption", "") or ""
        title = snap.get("title", "") or ""
        cta_text = snap.get("cta_text", "") or ""
        start_date = ad.get("start_date") or ""

        # Convert unix timestamp to ISO if needed
        if isinstance(start_date, (int, float)):
            from datetime import datetime, timezone
            start_date = datetime.fromtimestamp(start_date, tz=timezone.utc).isoformat()

        sample = {
            "text": text[:300] if text else "",
            "link_url": link_url[:200] if link_url else "",
            "title": title[:150] if title else "",
            "cta_text": cta_text[:50] if cta_text else "",
            "start_date": str(start_date),
        }
        # Only include if there's at least some content
        if sample["text"] or sample["title"]:
            samples.append(sample)

    return {
        "has_ads": True,
        "ad_count": len(ads),
        "ad_samples": json.dumps(samples),
    }
