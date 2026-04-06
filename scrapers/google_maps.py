"""
Google Maps scraper using Apify's Google Places Crawler.
Finds home service businesses by niche + location.
"""
from __future__ import annotations

import logging
from apify_client import ApifyClient

import config

logger = logging.getLogger(__name__)


def build_search_queries(niches: list[str], locations: list[str]) -> list[str]:
    """Build search queries like 'roofing company in Miami, FL'."""
    queries = []
    for niche in niches:
        for location in locations:
            queries.append(f"{niche} in {location}")
    return queries


def scrape_google_maps(
    niches: list[str] | None = None,
    locations: list[str] | None = None,
    max_per_query: int | None = None,
) -> list[dict]:
    """
    Run the Google Maps scraper via Apify.

    Returns a list of business dicts with keys:
        name, phone, website, address, city, state, rating,
        review_count, category, search_query
    """
    if not config.APIFY_API_KEY:
        raise ValueError("APIFY_API_KEY not set in .env")

    client = ApifyClient(config.APIFY_API_KEY)

    if niches is None:
        niches = config.NICHES
    if locations is None:
        locations = []
        for state in config.ACTIVE_STATES:
            locations.extend(config.LOCATIONS.get(state, []))
    if max_per_query is None:
        max_per_query = config.MAX_RESULTS_PER_SEARCH

    queries = build_search_queries(niches, locations)
    logger.info(f"Running {len(queries)} search queries on Google Maps")

    all_results = []

    # Batch queries to avoid overloading (Apify handles concurrency)
    # The actor accepts multiple queries in one run
    batch_size = 50  # queries per actor run
    for i in range(0, len(queries), batch_size):
        batch = queries[i : i + batch_size]
        logger.info(
            f"Processing batch {i // batch_size + 1} "
            f"({len(batch)} queries)"
        )

        run_input = {
            "searchStringsArray": batch,
            "maxCrawledPlacesPerSearch": max_per_query,
            "language": "en",
            "maxImages": 0,
            "maxReviews": 0,
            "onlyDataFromSearchPage": False,
            "scrapeDirectories": False,
            "deeperCityScrape": False,
            "skipClosedPlaces": True,
        }

        try:
            run = client.actor(config.GOOGLE_MAPS_ACTOR_ID).call(
                run_input=run_input,
                timeout_secs=600,
            )

            dataset_items = client.dataset(
                run["defaultDatasetId"]
            ).list_items().items

            for item in dataset_items:
                business = normalize_result(item, batch)
                if business and business.get("website"):
                    all_results.append(business)

            logger.info(
                f"Batch yielded {len(dataset_items)} raw results, "
                f"{len(all_results)} total with websites"
            )

        except Exception as e:
            logger.error(f"Apify actor run failed for batch: {e}")
            continue

    logger.info(f"Google Maps scraping complete: {len(all_results)} businesses found")
    return all_results


def normalize_result(item: dict, queries: list[str]) -> dict | None:
    """Normalize an Apify result into our standard format."""
    name = item.get("title") or item.get("name")
    if not name:
        return None

    phone = item.get("phone") or item.get("phoneUnformatted")
    website = item.get("website")
    address = item.get("address") or item.get("street")
    city = item.get("city") or ""
    state = item.get("state") or ""

    return {
        "name": name.strip(),
        "phone": (phone or "").strip(),
        "website": (website or "").strip().rstrip("/") if website else "",
        "address": (address or "").strip(),
        "city": city.strip(),
        "state": state.strip(),
        "rating": item.get("totalScore") or item.get("rating") or 0,
        "review_count": item.get("reviewsCount") or item.get("reviews") or 0,
        "category": item.get("categoryName") or item.get("category") or "",
        "place_id": item.get("placeId") or "",
        "latitude": item.get("location", {}).get("lat") if item.get("location") else None,
        "longitude": item.get("location", {}).get("lng") if item.get("location") else None,
    }
