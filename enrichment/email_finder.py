"""
Email finder for decision makers.

Multi-strategy approach:
1. Scrape website for direct emails
2. Generate email permutations from name + domain
3. Verify emails via SMTP check (lightweight, no send)
4. Hunter.io API (paid, high quality)
5. Apify contact-info-scraper (cheap batch fallback)
"""
from __future__ import annotations

import logging
import re
import smtplib
import socket
import time
import dns.resolver
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}

# Generic emails to skip — these are NOT decision makers
GENERIC_PREFIXES = {
    "info", "contact", "hello", "support", "help", "sales",
    "admin", "office", "service", "billing", "customerservice",
    "noreply", "no-reply", "webmaster", "mail", "enquiries",
    "inquiries", "team", "general", "reception", "marketing",
}


def find_email(
    website: str,
    person_name: str | None = None,
) -> dict:
    """
    Find the best email for a business, prioritizing decision maker emails.

    Returns: {
        "email": "john@company.com",
        "email_type": "decision_maker" | "personal_pattern" | "generic",
        "confidence": "high" | "medium" | "low",
        "method": "website_scrape" | "pattern_generated" | "pattern_verified",
    }
    """
    if not website:
        return {}

    if not website.startswith(("http://", "https://")):
        website = "https://" + website

    domain = extract_domain(website)
    if not domain:
        return {}

    # Step 1: Scrape the website for emails
    scraped_emails = scrape_emails_from_website(website)

    # Separate personal vs generic emails
    personal_emails = []
    generic_emails = []
    for email in scraped_emails:
        if is_on_domain(email, domain):
            prefix = email.split("@")[0].lower()
            if prefix in GENERIC_PREFIXES:
                generic_emails.append(email)
            else:
                personal_emails.append(email)

    # If we found a personal email on the company domain, great
    if personal_emails:
        best = personal_emails[0]
        return {
            "email": best,
            "email_type": "decision_maker",
            "confidence": "high",
            "method": "website_scrape",
        }

    # Step 2: If we have a person's name, generate email patterns
    if person_name:
        pattern_emails = generate_email_patterns(person_name, domain)

        # Try SMTP verification with a short overall timeout
        verified = []
        try:
            import signal
            old_handler = signal.signal(signal.SIGALRM, lambda s, f: (_ for _ in ()).throw(TimeoutError))
            signal.alarm(15)  # 15s max for all SMTP checks
            verified = verify_emails_smtp(pattern_emails, domain)
        except (TimeoutError, Exception):
            pass
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_handler)

        if verified:
            return {
                "email": verified[0],
                "email_type": "personal_pattern",
                "confidence": "medium",
                "method": "pattern_verified",
            }

        # Even without SMTP verification, return the most common pattern
        if pattern_emails:
            return {
                "email": pattern_emails[0],  # first@domain is most common for small biz
                "email_type": "personal_pattern",
                "confidence": "low",
                "method": "pattern_generated",
            }

    # Step 3: Fall back to generic email
    if generic_emails:
        return {
            "email": generic_emails[0],
            "email_type": "generic",
            "confidence": "high",
            "method": "website_scrape",
        }

    return {}


def scrape_emails_from_website(website: str) -> list[str]:
    """Scrape a website (homepage + contact page) for email addresses."""
    emails = set()

    base = website.rstrip("/")
    pages_to_check = [
        website,
        base + "/contact",
        base + "/contact-us",
        base + "/about",
        base + "/about-us",
        base + "/team",
        base + "/our-team",
        base + "/our-story",
    ]

    for url in pages_to_check:
        # Early exit: if we already found a non-generic email, stop crawling
        non_generic = [e for e in emails if e.split("@")[0].lower() not in GENERIC_PREFIXES]
        if non_generic:
            break

        try:
            resp = requests.get(
                url, headers=HEADERS, timeout=8, allow_redirects=True
            )
            if resp.status_code != 200:
                continue

            # Find emails in HTML
            found = re.findall(
                r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
                resp.text,
            )
            emails.update(e.lower() for e in found)

            # Decode obfuscated emails: [at], (at), " at ", [dot], (dot)
            deobfuscated = resp.text
            deobfuscated = re.sub(r"\s*[\[(]\s*at\s*[\])]\s*", "@", deobfuscated, flags=re.IGNORECASE)
            deobfuscated = re.sub(r"\s*[\[(]\s*dot\s*[\])]\s*", ".", deobfuscated, flags=re.IGNORECASE)
            deobfuscated = re.sub(r"\s+at\s+", "@", deobfuscated)
            deobfuscated = re.sub(r"\s+dot\s+", ".", deobfuscated)
            found_deobf = re.findall(
                r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
                deobfuscated,
            )
            emails.update(e.lower() for e in found_deobf)

            # Also check mailto: links
            soup = BeautifulSoup(resp.text, "lxml")
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if href.startswith("mailto:"):
                    email = href.replace("mailto:", "").split("?")[0].strip().lower()
                    if "@" in email:
                        emails.add(email)

            # Check data-email / data-encrypted-mail attributes
            for el in soup.find_all(attrs={"data-email": True}):
                emails.add(el["data-email"].strip().lower())
            for el in soup.find_all(attrs={"data-cfemail": True}):
                decoded = _decode_cf_email(el["data-cfemail"])
                if decoded:
                    emails.add(decoded.lower())

            time.sleep(0.3)
        except requests.RequestException:
            continue

    # Filter out image files and junk
    valid = [
        e for e in emails
        if not e.endswith((".png", ".jpg", ".gif", ".svg", ".webp"))
        and len(e) < 80
        and "example.com" not in e
        and "sentry" not in e
    ]

    return valid


def generate_email_patterns(name: str, domain: str) -> list[str]:
    """
    Generate common email patterns from a person's name.
    Ordered by most common patterns for small businesses.
    """
    parts = name.lower().strip().split()
    if len(parts) < 2:
        return []

    first = re.sub(r"[^a-z]", "", parts[0])
    last = re.sub(r"[^a-z]", "", parts[-1])

    if not first or not last:
        return []

    return [
        f"{first}@{domain}",              # john@domain.com (very common for small biz)
        f"{first}{last}@{domain}",         # johnsmith@domain.com
        f"{first}.{last}@{domain}",        # john.smith@domain.com
        f"{first[0]}{last}@{domain}",      # jsmith@domain.com
        f"{first}{last[0]}@{domain}",      # johns@domain.com
        f"{first}_{last}@{domain}",        # john_smith@domain.com
        f"{first[0]}.{last}@{domain}",     # j.smith@domain.com
        f"{last}@{domain}",               # smith@domain.com
        f"{last}{first}@{domain}",         # smithjohn@domain.com
        f"{last}.{first}@{domain}",        # smith.john@domain.com
    ]


def verify_emails_smtp(emails: list[str], domain: str) -> list[str]:
    """
    Verify emails exist using SMTP RCPT TO check.
    Non-invasive — does not send any email.

    Returns list of verified emails.
    """
    if not emails:
        return []

    # Get MX records for the domain
    try:
        mx_records = dns.resolver.resolve(domain, "MX")
        mx_host = str(sorted(mx_records, key=lambda r: r.preference)[0].exchange).rstrip(".")
    except Exception:
        logger.debug(f"Could not resolve MX for {domain}")
        return []

    verified = []
    for email in emails[:5]:  # only check top 5 patterns
        try:
            with smtplib.SMTP(timeout=5) as smtp:
                smtp.connect(mx_host, 25)
                smtp.helo("bookedly.com")
                smtp.mail("verify@bookedly.com")
                code, _ = smtp.rcpt(email)
                if code == 250:
                    verified.append(email)
                smtp.quit()
        except (smtplib.SMTPException, socket.error, OSError, TimeoutError):
            continue
        time.sleep(0.2)

    return verified


# ── Hunter.io API ─────────────────────────────────────────────────────

def find_email_hunter(domain: str, person_name: str | None = None) -> dict:
    """
    Find email via Hunter.io API.
    Uses Email Finder (name+domain) if name available, else Domain Search.
    Returns same format as find_email().
    """
    import config
    api_key = config.HUNTER_API_KEY
    if not api_key:
        return {}

    try:
        if person_name and person_name.strip():
            # Email Finder — 1 credit, returns specific person's email
            parts = person_name.strip().split()
            first = parts[0]
            last = parts[-1] if len(parts) > 1 else ""
            resp = requests.get(
                "https://api.hunter.io/v2/email-finder",
                params={
                    "domain": domain,
                    "first_name": first,
                    "last_name": last,
                    "api_key": api_key,
                },
                timeout=12,
            )
            if resp.status_code == 200:
                data = resp.json().get("data", {})
                email = data.get("email")
                score = data.get("score", 0)
                if email and score >= 50:
                    return {
                        "email": email,
                        "email_type": "decision_maker",
                        "confidence": "high" if score >= 80 else "medium",
                        "method": "hunter_finder",
                    }

        # Domain Search — find any executive email on the domain
        resp = requests.get(
            "https://api.hunter.io/v2/domain-search",
            params={
                "domain": domain,
                "type": "personal",
                "seniority": "executive",
                "limit": 5,
                "api_key": api_key,
            },
            timeout=12,
        )
        if resp.status_code == 200:
            data = resp.json().get("data", {})
            emails = data.get("emails", [])
            for e in emails:
                if e.get("confidence", 0) >= 50:
                    name_parts = []
                    if e.get("first_name"):
                        name_parts.append(e["first_name"])
                    if e.get("last_name"):
                        name_parts.append(e["last_name"])
                    return {
                        "email": e["value"],
                        "email_type": "decision_maker",
                        "confidence": "high" if e["confidence"] >= 80 else "medium",
                        "method": "hunter_domain",
                        "hunter_name": " ".join(name_parts) if name_parts else "",
                        "hunter_position": e.get("position", ""),
                    }
    except Exception as e:
        logger.debug(f"Hunter.io failed for {domain}: {e}")

    return {}


# ── Apify contact-info-scraper (batch) ────────────────────────────────

def find_email_apify_contact(website: str) -> dict:
    """
    Use Apify contact-info-scraper to find emails from a website.
    Costs ~$0.001/site. Good for sites that hide emails in JS.
    """
    import config
    if not config.APIFY_API_KEY:
        return {}

    try:
        from apify_client import ApifyClient
        client = ApifyClient(config.APIFY_API_KEY)

        if not website.startswith("http"):
            website = "https://" + website

        run = client.actor("vdrmota/contact-info-scraper").call(
            run_input={
                "startUrls": [{"url": website}],
                "maxRequestsPerStartUrl": 5,
            },
            timeout_secs=60,
        )

        items = client.dataset(run["defaultDatasetId"]).list_items().items
        if not items:
            return {}

        # Collect all emails across crawled pages
        all_emails = []
        for item in items:
            all_emails.extend(item.get("emails", []))

        if not all_emails:
            return {}

        # Prefer non-generic
        generic_prefixes = {
            "info", "contact", "hello", "support", "help", "sales",
            "admin", "office", "service", "noreply", "no-reply",
        }
        personal = [e for e in all_emails if e.split("@")[0].lower() not in generic_prefixes]
        best = personal[0] if personal else all_emails[0]

        return {
            "email": best.lower(),
            "email_type": "personal" if personal else "generic",
            "confidence": "high",
            "method": "apify_contact",
        }
    except Exception as e:
        logger.debug(f"Apify contact scraper failed for {website}: {e}")
    return {}


def _decode_cf_email(encoded: str) -> str:
    """Decode Cloudflare's email obfuscation (data-cfemail attribute)."""
    try:
        r = int(encoded[:2], 16)
        return "".join(chr(int(encoded[i:i+2], 16) ^ r) for i in range(2, len(encoded), 2))
    except (ValueError, IndexError):
        return ""


def extract_domain(url: str) -> str:
    """Extract the root domain from a URL."""
    try:
        parsed = urlparse(url)
        domain = parsed.netloc or parsed.path.split("/")[0]
        domain = domain.lower().strip()
        if domain.startswith("www."):
            domain = domain[4:]
        return domain
    except Exception:
        return ""


def is_on_domain(email: str, domain: str) -> bool:
    """Check if an email belongs to a given domain."""
    email_domain = email.split("@")[-1].lower()
    # Handle www prefix
    if email_domain.startswith("www."):
        email_domain = email_domain[4:]
    return email_domain == domain or email_domain.endswith("." + domain)
