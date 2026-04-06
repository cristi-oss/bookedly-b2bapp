#!/usr/bin/env python3
"""
Bookedly Lead Scraper — Daily Pipeline

Automated lead generation for home service businesses:
1. Scrape Google Maps (1 state/day rotation)
2. Enrich with decision makers + emails
3. Check Facebook Ad Library
4. Generate personalized outreach angles
5. Export per-niche CSVs for Instantly

Usage:
    python main.py                  # Run full pipeline (today's state)
    python main.py --state florida  # Force a specific state
    python main.py --scrape-only    # Only scrape, no enrichment
    python main.py --enrich-only    # Only enrich existing leads without email
    python main.py --ads-only       # Only check FB ads for leads missing ad data
    python main.py --deliver-only   # Only export CSVs
    python main.py --stats          # Show database stats
    python main.py --schedule       # Run on daily schedule
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import schedule

import config
from scrapers.google_maps import scrape_google_maps
from scrapers.facebook_ads import check_facebook_ads
from enrichment.website_scraper import find_decision_makers
from enrichment.email_finder import find_email, find_email_apify_contact, extract_domain
from enrichment.ad_analyzer import generate_angle
from storage.leads_db import (
    init_db,
    insert_leads_batch,
    get_undelivered_leads,
    get_leads_without_ads,
    get_leads_by_niche,
    update_lead_ads,
    mark_delivered,
    get_stats,
    get_connection,
)
from delivery.csv_export import export_leads_csv, export_by_niche, export_for_instantly
from delivery.slack import send_daily_report, send_slack_message
from enrichment.email_verifier import verify_unverified_leads, get_verified_leads_for_upload
from delivery.instantly import upload_leads, list_campaigns

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("lead_scraper.log"),
    ],
)
logger = logging.getLogger(__name__)


def _save_scrape_file(businesses: list[dict], state: str, niche_filter: str | None = None):
    """Save a CSV snapshot of each scrape run, named with date/state/niche."""
    import csv
    os.makedirs(config.CSV_OUTPUT_DIR, exist_ok=True)

    date_str = datetime.utcnow().strftime("%Y-%m-%d_%H%M")
    niche_tag = niche_filter.replace(" ", "-") if niche_filter else "all-niches"
    filename = f"scrape_{date_str}_{state}_{niche_tag}_{len(businesses)}leads.csv"
    filepath = os.path.join(config.CSV_OUTPUT_DIR, filename)

    columns = [
        "name", "phone", "website", "address", "city", "state", "niche",
        "rating", "review_count", "category", "email", "decision_maker_name",
        "outreach_angle", "angle_type",
    ]

    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for biz in businesses:
            writer.writerow(biz)

    logger.info(f"Scrape snapshot saved: {filepath}")
    return filepath


def get_active_state(forced_state: str | None = None) -> str:
    """Determine which state to scrape today."""
    if forced_state:
        return forced_state
    if config.ACTIVE_STATES:
        return config.ACTIVE_STATES[0]
    return config.get_today_state()


def run_scrape(
    state: str,
    max_leads: int | None = None,
    niche_filter: str | None = None,
    city_filter: str | None = None,
) -> list[dict]:
    """Step 1: Scrape Google Maps for businesses in a state."""
    locations = config.LOCATIONS.get(state, [])
    if not locations:
        logger.warning(f"No locations configured for state: {state}")
        return []

    # Apply city filter
    if city_filter:
        locations = [loc for loc in locations if city_filter.lower() in loc.lower()]
        if not locations:
            logger.warning(f"City '{city_filter}' not found in {state} locations")
            return []

    # Apply niche filter
    niches = config.NICHES
    if niche_filter:
        niches = [n for n in config.NICHES if niche_filter.lower() in n.lower()]
        if not niches:
            logger.warning(f"Niche '{niche_filter}' not found in config")
            return []

    logger.info(f"=== STEP 1: Scraping Google Maps — {state.upper()} ===")
    logger.info(f"  Niches: {len(niches)} | Cities: {len(locations)}")

    all_businesses = []
    seen_websites: set[str] = set()

    for niche in niches:
        niche_label = config.NICHE_LABELS.get(niche, niche)

        if max_leads and len(all_businesses) >= max_leads:
            logger.info(f"Reached {len(all_businesses)} leads — stopping early (max={max_leads})")
            break

        logger.info(f"Scraping '{niche}' in {len(locations)} cities ({len(all_businesses)} leads so far)...")

        try:
            businesses = scrape_google_maps(
                niches=[niche],
                locations=locations,
            )
            for biz in businesses:
                website = (biz.get("website") or "").rstrip("/")
                if website and website not in seen_websites:
                    seen_websites.add(website)
                    biz["niche"] = niche_label
                    all_businesses.append(biz)
        except Exception as e:
            logger.error(f"Scrape failed for {niche} in {state}: {e}")

    logger.info(f"Scraped {len(all_businesses)} unique businesses in {state}")
    return all_businesses


def _enrich_single(biz: dict) -> dict:
    """
    Streamlined enrichment: website scrape → email finder.
    Apify contact-info-scraper runs as a batch after (see run_enrich_apify_batch).
    """
    website = biz.get("website", "")
    name = biz.get("name", "")
    dm_name = ""
    dm_title = ""
    email_result = {}

    # ── Stage 1: Website scrape for DM name + title ──
    try:
        decision_makers = find_decision_makers(website, business_name=name)
        if decision_makers:
            dm_name = decision_makers[0]["name"]
            dm_title = decision_makers[0]["title"]
    except Exception as e:
        logger.debug(f"Website DM lookup failed for {website}: {e}")

    # ── Stage 2: Find email (scrapes site + generates patterns if name found) ──
    try:
        email_result = find_email(website=website, person_name=dm_name or None)
    except Exception as e:
        logger.debug(f"Email lookup failed for {website}: {e}")

    # Apify contact-info-scraper runs as batch in run_enrich_apify_batch()
    biz.update(_build_enrichment(dm_name, dm_title, email_result))

    # ── Stage 3 (inline fallback): generic info@ address from domain ──
    domain = extract_domain(website if website.startswith("http") else "https://" + website) if website else ""
    if not biz.get("email") and domain:
        biz["email"] = f"info@{domain}"
        biz["email_type"] = "generic_fallback"
        biz["email_confidence"] = "low"
        biz["email_method"] = "domain_fallback"
        logger.debug(f"Using domain fallback email for {domain}")

    return biz


def _build_enrichment(dm_name: str, dm_title: str, email_result: dict) -> dict:
    """Build the enrichment fields dict."""
    return {
        "decision_maker_name": dm_name,
        "decision_maker_title": dm_title,
        "email": email_result.get("email", ""),
        "email_type": email_result.get("email_type", ""),
        "email_confidence": email_result.get("confidence", ""),
        "email_method": email_result.get("method", ""),
    }


def run_enrich(businesses: list[dict], workers: int = 10) -> list[dict]:
    """Step 2: Parallel enrichment for decision makers + emails."""
    to_enrich = [b for b in businesses if b.get("website")]
    total = len(to_enrich)
    logger.info(f"=== STEP 2: Enriching {total} businesses ({workers} workers) ===")

    enriched = []
    done = 0

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_enrich_single, biz): biz for biz in to_enrich}
        for future in as_completed(futures):
            done += 1
            try:
                result = future.result()
                enriched.append(result)
            except Exception as e:
                biz = futures[future]
                logger.debug(f"Enrich failed for {biz.get('website')}: {e}")
                enriched.append(biz)

            if done % 50 == 0:
                dm_count = sum(1 for b in enriched if b.get("decision_maker_name"))
                em_count = sum(1 for b in enriched if b.get("email"))
                logger.info(f"Enriched {done}/{total}... (DMs: {dm_count}, Emails: {em_count})")

    with_dm = sum(1 for b in enriched if b.get("decision_maker_name"))
    with_email = sum(1 for b in enriched if b.get("email"))
    logger.info(f"Enrichment complete: {with_dm}/{total} DMs, {with_email}/{total} emails")
    return enriched


def run_enrich_existing():
    """Re-enrich leads in DB that don't have emails yet, using full waterfall."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM leads WHERE (email IS NULL OR email = '') LIMIT 500"
    ).fetchall()
    conn.close()

    if not rows:
        logger.info("No unenriched leads in database")
        return 0

    leads = [dict(r) for r in rows]
    logger.info(f"Re-enriching {len(leads)} leads without emails (waterfall)")

    enriched_count = 0
    for lead in leads:
        website = lead.get("website", "")
        if not website:
            continue

        try:
            result = _enrich_single(lead)
            if result.get("email"):
                conn = get_connection()
                conn.execute(
                    """UPDATE leads SET
                        decision_maker_name = ?,
                        decision_maker_title = ?,
                        email = ?,
                        email_type = ?,
                        email_confidence = ?,
                        email_method = ?
                    WHERE id = ?""",
                    (
                        result.get("decision_maker_name", ""),
                        result.get("decision_maker_title", ""),
                        result["email"],
                        result.get("email_type", ""),
                        result.get("email_confidence", ""),
                        result.get("email_method", ""),
                        lead["id"],
                    ),
                )
                conn.commit()
                conn.close()
                enriched_count += 1
        except Exception as e:
            logger.debug(f"Re-enrichment failed for {website}: {e}")

        time.sleep(0.5)

    logger.info(f"Re-enriched {enriched_count} leads with emails")
    return enriched_count


def run_enrich_apify_batch(enriched: list[dict]) -> int:
    """
    Batch Apify contact scraper for leads that still have no *real* email
    (i.e. still on the generic_fallback placeholder or truly empty).
    Runs after the main enrichment loop for efficiency.
    Mutates the dicts in-place so the caller's list is updated automatically.
    Returns the number of leads upgraded with an Apify-found email.
    """
    # Target leads that are still on the domain-fallback placeholder or have no email at all
    no_real_email = [
        b for b in enriched
        if b.get("website") and (
            not b.get("email") or b.get("email_method") == "domain_fallback"
        )
    ]
    if not no_real_email:
        return 0

    logger.info(f"=== APIFY BATCH: {len(no_real_email)} leads still without real email ===")
    found = 0

    for biz in no_real_email:
        try:
            result = find_email_apify_contact(biz["website"])
            if result and result.get("email"):
                biz["email"] = result["email"]
                biz["email_type"] = result.get("email_type", "")
                biz["email_confidence"] = result.get("confidence", "")
                biz["email_method"] = result.get("method", "")
                found += 1
        except Exception as e:
            logger.debug(f"Apify batch failed for {biz['website']}: {e}")

    logger.info(f"Apify batch found {found} more emails")
    return found


def run_facebook_ads():
    """Step 3: Check Facebook Ad Library for leads without ad data."""
    logger.info("=== STEP 3: Checking Facebook Ad Library ===")

    leads = get_leads_without_ads(limit=200)
    if not leads:
        logger.info("All leads already checked for Facebook ads")
        return 0

    logger.info(f"Checking {len(leads)} leads for Facebook ads...")
    ad_results = check_facebook_ads(leads)

    updated = 0
    for ad_data in ad_results:
        lead_id = ad_data.get("lead_id")
        if not lead_id:
            continue

        # Find the matching lead for angle generation
        lead = next((l for l in leads if l.get("id") == lead_id), {})

        # Generate outreach angle
        angle_data = generate_angle(lead, ad_data)

        # Map scraper field names → DB field names
        db_data = {
            "has_facebook_ads": 1 if ad_data.get("has_ads") else 0,
            "facebook_ad_count": ad_data.get("ad_count", 0),
            "facebook_ad_samples": ad_data.get("ad_samples", "[]"),
            "ad_analysis": angle_data.get("ad_analysis", ""),
            "subject_line": angle_data.get("subject_line", ""),
            "outreach_angle": angle_data.get("outreach_angle", ""),
            "angle_type": angle_data.get("angle_type", ""),
        }

        update_lead_ads(lead_id, db_data)
        updated += 1

    logger.info(f"Updated {updated} leads with Facebook ad data + angles")
    return updated


def run_deliver(limit: int | None = None, campaign_id: str | None = None) -> list[str]:
    """Step 6: Upload verified leads to Instantly + export CSVs."""
    logger.info("=== STEP 6: DELIVERING LEADS ===")

    # Prefer verified leads; fall back to all undelivered if verification hasn't run
    leads = get_verified_leads_for_upload(limit=limit or config.DAILY_EMAIL_TARGET)
    if not leads:
        # Fallback: grab undelivered leads even if not yet verified
        leads = get_undelivered_leads(limit=limit or config.DAILY_EMAIL_TARGET)

    if not leads:
        logger.info("No new leads to deliver")
        send_slack_message(
            ":information_source: *Bookedly Lead Scraper*\nNo new leads to deliver today."
        )
        return []

    logger.info(f"Delivering {len(leads)} leads...")

    # ── Upload to Instantly ──
    if config.INSTANTLY_API_KEY and campaign_id:
        try:
            result = upload_leads(campaign_id, leads, mark_as_delivered=True)
            logger.info(
                "Instantly upload: uploaded=%d skipped=%d failed=%d",
                result["uploaded"], result["skipped"], result["failed"],
            )
        except Exception as e:
            logger.error(f"Instantly upload failed: {e}")
    elif config.INSTANTLY_API_KEY and not campaign_id:
        # Auto-detect: use the first active campaign
        try:
            campaigns = list_campaigns()
            active = [c for c in campaigns if c.get("status") in ("active", "running")]
            if active:
                campaign_id = active[0]["id"]
                logger.info(f"Auto-selected Instantly campaign: {active[0]['name']} ({campaign_id})")
                result = upload_leads(campaign_id, leads, mark_as_delivered=True)
                logger.info(
                    "Instantly upload: uploaded=%d skipped=%d failed=%d",
                    result["uploaded"], result["skipped"], result["failed"],
                )
            else:
                logger.warning("No active Instantly campaigns found — skipping upload")
        except Exception as e:
            logger.error(f"Instantly upload failed: {e}")

    # ── Export CSVs ──
    csv_path = export_leads_csv(leads)
    niche_paths = export_by_niche(leads)
    instantly_path = export_for_instantly(leads)

    # Mark as delivered (if not already marked by Instantly upload)
    undelivered_ids = [l["id"] for l in leads if not l.get("delivered_at")]
    if undelivered_ids:
        mark_delivered(undelivered_ids)

    # Stats + Slack report
    stats = get_stats()
    stats["new_today"] = len(leads)
    send_daily_report(stats, csv_path)

    all_paths = [csv_path, instantly_path] + niche_paths
    logger.info(f"Delivered {len(leads)} leads → {len(niche_paths)} niche files")
    return all_paths


def run_full_pipeline(
    forced_state: str | None = None,
    max_leads: int | None = None,
    niche_filter: str | None = None,
    city_filter: str | None = None,
    no_deliver: bool = False,
):
    """
    Run the complete daily pipeline in bulk stages:
      1. Scrape ALL leads → save to DB immediately
      2. Enrich leads from DB (parallel)
      3. Apify batch fallback for missing emails
      4. Verify emails
      5. Facebook ads check
      6. Export & deliver
    """
    state = get_active_state(forced_state)

    logger.info("=" * 60)
    logger.info("BOOKEDLY LEAD SCRAPER — FULL PIPELINE")
    logger.info(f"Date: {datetime.utcnow().isoformat()}")
    logger.info(f"Today's state: {state.upper()}")
    logger.info(f"Niches: {len(config.NICHES)}")
    if max_leads:
        logger.info(f"Max leads target: {max_leads}")
    logger.info("=" * 60)

    start = time.time()

    # ── Stage 1: Scrape ALL leads from Google Maps ──
    businesses = run_scrape(state, max_leads=max_leads, niche_filter=niche_filter, city_filter=city_filter)
    if not businesses:
        logger.warning("No businesses scraped — stopping pipeline")
        return

    # Save raw leads to DB immediately (no enrichment yet)
    new_count = insert_leads_batch(businesses)
    logger.info(f"Stage 1 done: saved {new_count} new leads to DB ({len(businesses) - new_count} dupes skipped)")

    # Save a scrape snapshot CSV so every run is downloadable from the dashboard
    _save_scrape_file(businesses, state, niche_filter)

    # ── Stage 2: Enrich all leads without emails (from DB) ──
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM leads WHERE (email IS NULL OR email = '') LIMIT ?",
        (max_leads or 2000,),
    ).fetchall()
    conn.close()
    unenriched = [dict(r) for r in rows]

    if unenriched:
        logger.info(f"Stage 2: enriching {len(unenriched)} leads from DB...")
        enriched = run_enrich(unenriched)

        # Apply generic fallback for any still without email
        fallback_applied = 0
        for biz in enriched:
            if not biz.get("email") and biz.get("website"):
                website = biz["website"]
                if not website.startswith("http"):
                    website = "https://" + website
                domain = extract_domain(website)
                if domain:
                    biz["email"] = f"info@{domain}"
                    biz["email_type"] = "generic_fallback"
                    biz["email_confidence"] = "low"
                    biz["email_method"] = "domain_fallback"
                    fallback_applied += 1
        if fallback_applied:
            logger.info(f"Generic fallback applied to {fallback_applied} leads")

        # Update DB with enrichment results
        conn = get_connection()
        updated = 0
        for biz in enriched:
            if biz.get("email") and biz.get("id"):
                conn.execute(
                    """UPDATE leads SET
                        decision_maker_name = ?,
                        decision_maker_title = ?,
                        email = ?,
                        email_type = ?,
                        email_confidence = ?,
                        email_method = ?
                    WHERE id = ?""",
                    (
                        biz.get("decision_maker_name", ""),
                        biz.get("decision_maker_title", ""),
                        biz["email"],
                        biz.get("email_type", ""),
                        biz.get("email_confidence", ""),
                        biz.get("email_method", ""),
                        biz["id"],
                    ),
                )
                updated += 1
        conn.commit()
        conn.close()
        logger.info(f"Stage 2 done: updated {updated} leads with enrichment data")
    else:
        logger.info("Stage 2: no unenriched leads in DB — skipping")

    # ── Stage 3: Apify batch for leads still missing real email ──
    conn = get_connection()
    rows = conn.execute(
        """SELECT * FROM leads
        WHERE website IS NOT NULL AND website != ''
        AND (email IS NULL OR email = '' OR email_method = 'domain_fallback')
        LIMIT ?""",
        (max_leads or 2000,),
    ).fetchall()
    conn.close()
    still_missing = [dict(r) for r in rows]

    if still_missing:
        apify_found = run_enrich_apify_batch(still_missing)
        if apify_found:
            conn = get_connection()
            for biz in still_missing:
                if biz.get("email") and biz.get("email_method") == "apify_contact" and biz.get("id"):
                    conn.execute(
                        """UPDATE leads SET email = ?, email_type = ?,
                            email_confidence = ?, email_method = ?
                        WHERE id = ?""",
                        (biz["email"], biz.get("email_type", ""),
                         biz.get("email_confidence", ""), biz["email_method"], biz["id"]),
                    )
            conn.commit()
            conn.close()
            logger.info(f"Stage 3 done: Apify batch found {apify_found} more emails")

    # ── Stage 4: Verify emails ──
    logger.info("=== Stage 4: Verifying emails ===")
    verify_results = verify_unverified_leads(limit=max_leads or 1000)
    logger.info(f"Verification: {verify_results}")

    # ── Stage 5: Facebook ads check ──
    ads_checked = run_facebook_ads()

    # ── Stage 6: Export & deliver ──
    if no_deliver:
        logger.info("=== SKIPPING Stage 6 (--no-deliver) — leads ready but NOT sent to Instantly ===")
        # Still export a final CSV with all enrichment + angles
        _save_scrape_file(
            [dict(r) for r in get_connection().execute(
                "SELECT * FROM leads ORDER BY created_at DESC LIMIT ?", (max_leads or 2000,)
            ).fetchall()],
            state,
            niche_filter,
        )
        elapsed = time.time() - start
        logger.info(f"Pipeline complete in {elapsed / 60:.1f} minutes (no delivery)")
        stats = get_stats()
        logger.info(f"DB totals: {stats}")
        return

    paths = run_deliver()

    elapsed = time.time() - start
    logger.info(f"Pipeline complete in {elapsed / 60:.1f} minutes")
    logger.info(f"New leads: {new_count} | Ads checked: {ads_checked}")

    stats = get_stats()
    logger.info(f"DB totals: {stats}")


def run_scheduled():
    """Run the pipeline on a daily schedule."""
    logger.info("Starting scheduled mode — pipeline runs daily at 6:00 AM UTC")
    send_slack_message(
        ":rocket: *Bookedly Lead Scraper* started in scheduled mode.\n"
        "Pipeline will run daily at 6:00 AM UTC."
    )

    # Run once immediately
    run_full_pipeline()

    # Schedule daily
    schedule.every().day.at("06:00").do(run_full_pipeline)

    while True:
        schedule.run_pending()
        time.sleep(60)


def main():
    parser = argparse.ArgumentParser(description="Bookedly Lead Scraper")
    parser.add_argument("--state", type=str, help="Force a specific state to scrape")
    parser.add_argument("--scrape-only", action="store_true", help="Only scrape Google Maps")
    parser.add_argument("--enrich-only", action="store_true", help="Only enrich existing leads")
    parser.add_argument("--ads-only", action="store_true", help="Only check Facebook ads")
    parser.add_argument("--deliver-only", action="store_true", help="Only export CSVs")
    parser.add_argument("--stats", action="store_true", help="Show database statistics")
    parser.add_argument("--schedule", action="store_true", help="Run on daily schedule")
    parser.add_argument("--limit", type=int, help="Limit number of leads to deliver")
    parser.add_argument("--campaign-id", type=str, help="Instantly campaign UUID for auto-upload")
    parser.add_argument("--verify-only", action="store_true", help="Only verify unverified emails")
    parser.add_argument("--max-leads", type=int, help="Stop scraping after this many leads (cost control)")
    parser.add_argument("--niche", type=str, help="Scrape only this niche (e.g. 'roofing company')")
    parser.add_argument("--city", type=str, help="Scrape only this city (e.g. 'Miami, FL')")
    parser.add_argument("--no-deliver", action="store_true", help="Run pipeline but skip Instantly upload")
    args = parser.parse_args()

    init_db()

    if args.stats:
        stats = get_stats()
        print("\nBookedly Lead Scraper — Database Stats")
        print("=" * 40)
        for key, val in stats.items():
            print(f"  {key.replace('_', ' ').title()}: {val}")
        state = get_active_state(args.state)
        print(f"\n  Today's state: {state.upper()}")
        print(f"  States in rotation: {len(config.STATE_ROTATION)}")
        print()
        return

    if args.scrape_only:
        state = get_active_state(args.state)
        businesses = run_scrape(state, max_leads=args.max_leads, niche_filter=args.niche, city_filter=args.city)
        new_count = insert_leads_batch(businesses)
        if businesses:
            _save_scrape_file(businesses, state, args.niche)
        print(f"\nScraped & stored {new_count} new leads from {state.upper()} (no enrichment)")
        return

    if args.enrich_only:
        count = run_enrich_existing()
        print(f"\nRe-enriched {count} leads")
        return

    if args.ads_only:
        count = run_facebook_ads()
        print(f"\nChecked Facebook ads for {count} leads")
        return

    if args.verify_only:
        results = verify_unverified_leads(limit=args.limit or 500)
        print(f"\nVerification: {results}")
        return

    if args.deliver_only:
        paths = run_deliver(limit=args.limit, campaign_id=getattr(args, 'campaign_id', None))
        if paths:
            print(f"\nDelivered leads:")
            for p in paths:
                print(f"  {p}")
        else:
            print("\nNo leads to deliver")
        return

    if args.schedule:
        run_scheduled()
        return

    # Default: run full pipeline once
    run_full_pipeline(
        forced_state=args.state,
        max_leads=args.max_leads,
        niche_filter=args.niche,
        city_filter=args.city,
        no_deliver=args.no_deliver,
    )


if __name__ == "__main__":
    main()
