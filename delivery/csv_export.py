"""
CSV export for leads — generates per-niche lead files compatible with Instantly.
"""
from __future__ import annotations

import csv
import logging
import os
from datetime import datetime

import config

logger = logging.getLogger(__name__)

# Full column set including ad data
CSV_COLUMNS = [
    "name",
    "decision_maker_name",
    "decision_maker_title",
    "email",
    "email_type",
    "email_confidence",
    "phone",
    "website",
    "address",
    "city",
    "state",
    "niche",
    "category",
    "rating",
    "review_count",
    "has_facebook_ads",
    "facebook_ad_count",
    "subject_line",
    "outreach_angle",
    "angle_type",
    "ad_analysis",
]

# Instantly-compatible columns (custom vars use {{variable}} in templates)
INSTANTLY_COLUMNS = [
    "email",
    "first_name",
    "last_name",
    "company_name",
    "phone",
    "website",
    "city",
    "state",
    "niche",
    "rating",
    "review_count",
    "subject_line",
    "outreach_angle",
    "angle_type",
    "has_facebook_ads",
]


def export_leads_csv(leads: list[dict], filename: str | None = None) -> str:
    """Export leads to a CSV file. Returns the file path."""
    os.makedirs(config.CSV_OUTPUT_DIR, exist_ok=True)

    if filename is None:
        date_str = datetime.utcnow().strftime("%Y-%m-%d_%H%M")
        filename = f"leads_{date_str}.csv"

    filepath = os.path.join(config.CSV_OUTPUT_DIR, filename)

    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for lead in leads:
            writer.writerow(lead)

    logger.info(f"Exported {len(leads)} leads to {filepath}")
    return filepath


def export_by_niche(leads: list[dict]) -> list[str]:
    """
    Export leads into separate CSV files per niche.
    Returns list of file paths created.
    """
    os.makedirs(config.CSV_OUTPUT_DIR, exist_ok=True)

    # Group by niche
    by_niche: dict[str, list[dict]] = {}
    for lead in leads:
        niche = lead.get("niche") or "other"
        by_niche.setdefault(niche, []).append(lead)

    paths = []
    for niche, niche_leads in sorted(by_niche.items()):
        filename = f"{niche}_leads.csv"
        filepath = os.path.join(config.CSV_OUTPUT_DIR, filename)

        with open(filepath, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            for lead in niche_leads:
                writer.writerow(lead)

        logger.info(f"Exported {len(niche_leads)} {niche} leads to {filepath}")
        paths.append(filepath)

    return paths


def export_for_instantly(leads: list[dict], filename: str | None = None) -> str:
    """
    Export leads in Instantly-compatible CSV format.
    Splits decision_maker_name into first_name/last_name.
    """
    os.makedirs(config.CSV_OUTPUT_DIR, exist_ok=True)

    if filename is None:
        date_str = datetime.utcnow().strftime("%Y-%m-%d_%H%M")
        filename = f"instantly_{date_str}.csv"

    filepath = os.path.join(config.CSV_OUTPUT_DIR, filename)

    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=INSTANTLY_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for lead in leads:
            row = dict(lead)
            # Split decision maker name for Instantly personalization
            dm_name = lead.get("decision_maker_name", "") or ""
            parts = dm_name.strip().split()
            row["first_name"] = parts[0] if parts else ""
            row["last_name"] = " ".join(parts[1:]) if len(parts) > 1 else ""
            row["company_name"] = lead.get("name", "")
            writer.writerow(row)

    logger.info(f"Exported {len(leads)} leads for Instantly to {filepath}")
    return filepath
