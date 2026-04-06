"""
Website scraper to find decision makers (owners, founders, managers)
from company About/Team pages.
"""
from __future__ import annotations

import logging
import re
import time
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Pages likely to contain owner/team info
TEAM_PAGE_PATHS = [
    "/about",
    "/about-us",
    "/about-us/",
    "/about/",
    "/our-team",
    "/our-team/",
    "/team",
    "/team/",
    "/meet-the-team",
    "/staff",
    "/leadership",
    "/company",
    "/who-we-are",
]

# Title patterns indicating a decision maker
DECISION_MAKER_TITLES = [
    r"\bowner\b",
    r"\bfounder\b",
    r"\bco-?founder\b",
    r"\bceo\b",
    r"\bchief executive\b",
    r"\bpresident\b",
    r"\bprincipal\b",
    r"\bmanaging director\b",
    r"\bgeneral manager\b",
    r"\bdirector\b",
    r"\bpartner\b",
    r"\bproprietor\b",
]

# Regex to strip title suffixes that bleed into names
TITLE_SUFFIX_RE = re.compile(
    r"\s*[,\-–—|/]?\s*\b(?:co-?owner|owner|founder|co-?founder|ceo|coo|cfo|cto|"
    r"president|principal|director|manager|partner|proprietor|vp|vice\s+president|"
    r"general\s+manager|managing\s+director|chief)\b.*$",
    re.IGNORECASE,
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}

REQUEST_TIMEOUT = 15


def find_decision_makers(website: str, business_name: str = "") -> list[dict]:
    """
    Scrape a company website to find decision maker names and titles.

    Returns list of dicts: [{"name": "John Smith", "title": "Owner"}, ...]
    """
    if not website:
        return []

    website = normalize_url(website)
    decision_makers = []

    # First try the homepage
    homepage_dms = scrape_page_for_people(website)
    decision_makers.extend(homepage_dms)

    # Then try team/about pages
    for path in TEAM_PAGE_PATHS:
        if decision_makers:
            break  # found someone, no need to keep scraping

        url = urljoin(website, path)
        try:
            time.sleep(0.5)  # polite delay
            people = scrape_page_for_people(url)
            decision_makers.extend(people)
        except Exception:
            continue

    # Deduplicate by name and validate
    biz_words = set(business_name.lower().split()) if business_name else set()
    seen = set()
    unique = []
    for dm in decision_makers:
        name_key = dm["name"].lower().strip()
        if name_key in seen or len(name_key.split()) < 2:
            continue
        # Reject if 2+ words overlap with business name
        if biz_words and len(set(name_key.split()) & biz_words) >= 2:
            continue
        seen.add(name_key)
        unique.append(dm)

    return unique


def scrape_page_for_people(url: str) -> list[dict]:
    """Scrape a single page for people with decision maker titles."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        if resp.status_code != 200:
            return []
    except requests.RequestException:
        return []

    soup = BeautifulSoup(resp.text, "lxml")
    people = []

    # Strategy 1: Look for structured team sections
    # Common patterns: name in h2/h3/h4 with title in nearby p/span
    for heading in soup.find_all(["h2", "h3", "h4", "strong"]):
        text = heading.get_text(strip=True)
        if not text or len(text) > 80 or len(text) < 3:
            continue

        # Strip title suffixes that bleed into name text
        cleaned = TITLE_SUFFIX_RE.sub("", text).strip()
        if not cleaned or len(cleaned) < 3:
            continue

        # Check sibling/parent for title
        parent = heading.parent
        if parent:
            parent_text = parent.get_text(" ", strip=True)
            title = extract_title(parent_text) or (extract_title(text) if text != cleaned else None)
            if title and looks_like_name(cleaned):
                people.append({"name": clean_name(cleaned), "title": title})

    # Strategy 2: Look for "Owner: Name" or "Founded by Name" patterns
    full_text = soup.get_text(" ", strip=True)
    pattern_matches = extract_from_text_patterns(full_text)
    people.extend(pattern_matches)

    # Strategy 3: Look for schema.org structured data
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            import json
            data = json.loads(script.string or "")
            schema_people = extract_from_schema(data)
            people.extend(schema_people)
        except (json.JSONDecodeError, TypeError):
            continue

    # Strategy 4: Meta tags (author, etc.)
    for meta in soup.find_all("meta"):
        if meta.get("name", "").lower() in ("author", "owner"):
            content = meta.get("content", "").strip()
            if content and looks_like_name(content):
                people.append({"name": clean_name(content), "title": "Owner"})

    return people


def extract_title(text: str) -> str | None:
    """Extract a decision maker title from text."""
    text_lower = text.lower()
    for pattern in DECISION_MAKER_TITLES:
        match = re.search(pattern, text_lower)
        if match:
            return match.group(0).title()
    return None


def extract_from_text_patterns(text: str) -> list[dict]:
    """Extract names from common text patterns."""
    people = []

    patterns = [
        r"(?:owned|founded|started|led|run)\s+by\s+([A-Z][a-z]+\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)",
        r"(?:owner|founder|ceo|president)[:\s,]+([A-Z][a-z]+\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)",
        r"([A-Z][a-z]+\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)[,\s]+(?:owner|founder|ceo|president)",
    ]

    for pattern in patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            name = TITLE_SUFFIX_RE.sub("", match.group(1)).strip()
            if looks_like_name(name):
                people.append({"name": clean_name(name), "title": "Owner"})

    return people


def extract_from_schema(data) -> list[dict]:
    """Extract people from JSON-LD schema data."""
    people = []

    if isinstance(data, list):
        for item in data:
            people.extend(extract_from_schema(item))
        return people

    if not isinstance(data, dict):
        return people

    # Check for Person type
    schema_type = data.get("@type", "")
    if schema_type == "Person" or (isinstance(schema_type, list) and "Person" in schema_type):
        name = data.get("name", "")
        title = data.get("jobTitle", "") or data.get("roleName", "") or "Owner"
        if name and looks_like_name(name):
            people.append({"name": clean_name(name), "title": title})

    # Check for founder/employee fields
    for field in ("founder", "employee", "member", "author"):
        val = data.get(field)
        if val:
            if isinstance(val, list):
                for v in val:
                    people.extend(extract_from_schema(v))
            elif isinstance(val, dict):
                people.extend(extract_from_schema(val))

    return people


def looks_like_name(text: str) -> bool:
    """Strict check if text looks like a person's name."""
    text = text.strip()
    words = text.split()
    if len(words) < 2 or len(words) > 3:
        return False
    if len(text) > 40:
        return False
    # Must start with uppercase
    if not text[0].isupper():
        return False
    # Each word must start with uppercase (proper name)
    if not all(w[0].isupper() for w in words if len(w) > 1):
        return False
    # Should not contain common non-name words
    non_name = {
        # Articles/prepositions/pronouns
        "the", "and", "our", "your", "this", "that", "for", "with", "from",
        "are", "was", "has", "have", "its", "their", "his", "her",
        # Nav/CTA words
        "about", "home", "team", "contact", "reviews", "blog", "faq",
        "read", "more", "click", "here", "call", "today", "view", "see",
        "free", "get", "best", "top", "new", "all", "learn", "sign",
        "why", "what", "how", "where", "when", "who",
        "next", "back", "menu", "page", "submit", "send", "request",
        # Business structure
        "llc", "inc", "corp", "services", "solutions", "group", "pro",
        "company", "enterprise", "enterprises", "associates", "consulting",
        "warehouse", "supply", "supplies", "central", "custom", "customs",
        "property", "properties", "city", "county", "national", "american",
        "premier", "elite", "advanced", "superior", "premium", "classic",
        # Industry terms
        "service", "customer", "quality", "warranty", "quote", "estimate",
        "floor", "flooring", "epoxy", "coating", "coatings", "concrete",
        "garage", "roofing", "roof", "roofer", "roofers", "hvac",
        "solar", "panel", "panels", "remodel", "remodeling", "kitchen",
        "bathroom", "painting", "painter", "painters", "plumbing", "plumber",
        "electrical", "electrician", "fence", "fencing", "pool", "pools",
        "window", "windows", "door", "doors", "pressure", "washing",
        "landscaping", "landscape", "lawn", "tree", "siding", "gutter",
        "construction", "contractor", "contractors", "building", "builders",
        "commercial", "residential", "professional", "licensed", "insured",
        "frequently", "asked", "questions", "restoration", "repair",
        "installation", "maintenance", "removal", "replacement", "design",
        "surface", "surfaces", "colors", "color", "stone", "tile", "wood",
        "metal", "steel", "iron", "copper", "glass", "vinyl", "brick",
        # Tech/web junk
        "crm", "software", "technology", "digital", "media", "web",
        "website", "online", "system", "systems", "platform", "app",
    }
    if any(w.lower() in non_name for w in words):
        return False
    # Each word should be only alpha chars (with . - ' allowed), no camelCase/concat
    if not all(re.match(r"^[A-Za-z\.\-']+$", w) for w in words):
        return False
    # Reject camelCase or mid-word capitals (e.g. "FryeCo", "ImpactPros")
    for w in words:
        if len(w) > 2 and re.search(r"[a-z][A-Z]", w):
            return False
    # Each word should be 2-15 chars (real name length)
    if not all(2 <= len(w) <= 15 for w in words):
        return False
    # ALL CAPS = not a name (e.g. "CUSTOMER REVIEWS") — check any word
    if any(w == w.upper() and len(w) > 2 for w in words):
        return False
    # Reject adverbs/adjectives commonly found near names in text
    filler = {
        "originally", "formerly", "currently", "previously", "also",
        "recently", "just", "simply", "truly", "very", "really",
    }
    if any(w.lower() in filler for w in words):
        return False
    return True


def clean_name(name: str) -> str:
    """Clean up a name string."""
    name = name.strip()
    name = re.sub(r"\s+", " ", name)
    return name


def normalize_url(url: str) -> str:
    """Ensure URL has scheme."""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url
