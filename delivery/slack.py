"""
Slack delivery — sends lead CSV files and summary stats to Slack.
"""
from __future__ import annotations

import logging
import os

import requests

import config

logger = logging.getLogger(__name__)


def send_slack_message(text: str) -> bool:
    """Send a text message to Slack via webhook."""
    webhook_url = config.SLACK_WEBHOOK_URL
    if not webhook_url:
        logger.warning("SLACK_WEBHOOK_URL not set, skipping Slack notification")
        return False

    try:
        resp = requests.post(
            webhook_url,
            json={"text": text},
            timeout=15,
        )
        if resp.status_code == 200:
            return True
        else:
            logger.error(f"Slack webhook returned {resp.status_code}: {resp.text}")
            return False
    except requests.RequestException as e:
        logger.error(f"Slack send failed: {e}")
        return False


def send_slack_file(filepath: str, message: str) -> bool:
    """
    Send a file to Slack.
    Note: Webhook doesn't support file uploads — this sends a message
    with a link/summary. For actual file upload, you'd need a Slack Bot Token.

    For now, we send the summary + tell the user where the CSV is.
    """
    if not os.path.exists(filepath):
        logger.error(f"File not found: {filepath}")
        return False

    filename = os.path.basename(filepath)
    file_size = os.path.getsize(filepath)

    # Count lines (leads)
    with open(filepath, "r") as f:
        lead_count = sum(1 for _ in f) - 1  # subtract header

    text = (
        f"{message}\n\n"
        f":page_facing_up: *File:* `{filename}`\n"
        f":bar_chart: *Leads:* {lead_count}\n"
        f":floppy_disk: *Size:* {file_size / 1024:.1f} KB\n\n"
        f"_CSV saved locally. Set up Slack Bot Token for direct file uploads._"
    )

    return send_slack_message(text)


def send_daily_report(stats: dict, csv_path: str | None = None) -> bool:
    """Send the daily lead scraping report to Slack."""
    blocks = [
        ":robot_face: *Bookedly Lead Scraper — Daily Report*",
        "",
        f":busts_in_silhouette: *Total leads in DB:* {stats.get('total_leads', 0)}",
        f":email: *With email:* {stats.get('with_email', 0)}",
        f":dart: *Decision maker emails:* {stats.get('decision_maker_emails', 0)}",
        f":incoming_envelope: *Delivered:* {stats.get('delivered', 0)}",
        f":hourglass_flowing_sand: *Pending delivery:* {stats.get('pending_delivery', 0)}",
    ]

    if stats.get("new_today"):
        blocks.append(f":new: *New today:* {stats['new_today']}")
    if stats.get("enriched_today"):
        blocks.append(f":mag: *Enriched today:* {stats['enriched_today']}")

    if csv_path:
        blocks.append(f"\n:page_facing_up: CSV: `{os.path.basename(csv_path)}`")

    return send_slack_message("\n".join(blocks))
