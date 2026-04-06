"""
Phase 2 enrichment sources: State SOS, Google/LinkedIn, DuckDuckGo.

Waterfall approach — each function returns:
    {"name": "John Smith", "title": "Owner", "source": "colorado_sos"}
or None if not found.
"""
from __future__ import annotations

import json
import logging
import re
import time
from urllib.parse import quote_plus, urljoin

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

# ── Name validation (shared with website_scraper.py) ──────────────────

_NON_NAME_WORDS = {
    "the", "and", "our", "your", "this", "that", "for", "with", "from",
    "home", "service", "services", "company", "team", "floor", "flooring",
    "epoxy", "roof", "roofing", "solar", "llc", "inc", "corp", "pro",
    "solutions", "group", "construction", "contractor", "contractors",
    "plumbing", "painting", "hvac", "electrical", "fence", "pool",
    "window", "garage", "pressure", "washing", "landscaping", "remodeling",
    "commercial", "residential", "professional", "registered", "agent",
    "corporation", "service", "enterprise", "enterprises", "ct",
}


def _is_valid_person_name(name: str, company_name: str = "") -> bool:
    """Check if extracted text is a real person name."""
    words = name.strip().split()
    if len(words) < 2 or len(words) > 3:
        return False
    if len(name) > 40:
        return False
    if not all(w[0].isupper() for w in words if len(w) > 1):
        return False
    if any(w.lower() in _NON_NAME_WORDS for w in words):
        return False
    if not all(re.match(r"^[A-Za-z.\-']+$", w) for w in words):
        return False
    if not all(2 <= len(w) <= 15 for w in words):
        return False
    # Reject if any word is ALL CAPS (len > 2)
    if any(w == w.upper() and len(w) > 2 for w in words):
        return False
    # Reject camelCase
    if any(re.search(r"[a-z][A-Z]", w) for w in words):
        return False
    # Cross-check against company name
    if company_name:
        biz_words = set(company_name.lower().split())
        if len(set(w.lower() for w in words) & biz_words) >= 2:
            return False
    return True


# ── Known registered agent companies (not real owners) ────────────────

_RA_COMPANIES = {
    "ct corporation", "registered agents", "corporation service",
    "csc global", "nrai", "legalinc", "incorp services", "cogency global",
    "northwest registered", "harbor compliance", "spiegel accountancy",
    "united agent group", "the corporation trust",
}


def _is_ra_company(name: str) -> bool:
    """Check if name is a known registered agent service, not a person."""
    lower = name.lower()
    return any(ra in lower for ra in _RA_COMPANIES)


# ══════════════════════════════════════════════════════════════════════
#  STATE SOS SCRAPERS
# ══════════════════════════════════════════════════════════════════════

# Map state name -> scraper function
_STATE_SCRAPERS = {}


def register_state(state_name: str):
    """Decorator to register a state scraper function."""
    def decorator(func):
        _STATE_SCRAPERS[state_name.lower()] = func
        return func
    return decorator


def find_owner_from_state(company_name: str, state: str) -> dict | None:
    """Look up company owner from state business registration."""
    state_lower = state.lower().replace(" ", "_")
    scraper = _STATE_SCRAPERS.get(state_lower)
    if not scraper:
        return None
    try:
        result = scraper(company_name)
        if result and _is_ra_company(result.get("name", "")):
            return None  # skip registered agent companies
        return result
    except Exception as e:
        logger.debug(f"State SOS lookup failed for {company_name} in {state}: {e}")
        return None


def get_supported_states() -> list[str]:
    return list(_STATE_SCRAPERS.keys())


# ── Colorado: Open SODA API (no auth, JSON) ──────────────────────────

@register_state("colorado")
def _search_colorado(company_name: str) -> dict | None:
    """Colorado SOS via data.colorado.gov SODA API. Returns registered agent."""
    clean = company_name.upper().replace("'", "''")
    url = (
        "https://data.colorado.gov/resource/4ykn-tg5h.json"
        f"?$where=entityname like '%25{quote_plus(clean)}%25'"
        "&$limit=5&$order=entityformdate DESC"
    )
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        if resp.status_code != 200:
            return None
        data = resp.json()
        for record in data:
            first = (record.get("agentfirstname") or "").strip()
            last = (record.get("agentlastname") or "").strip()
            if not first or not last:
                continue
            name = f"{first.title()} {last.title()}"
            if _is_valid_person_name(name, company_name):
                return {"name": name, "title": "Registered Agent", "source": "colorado_sos"}
    except Exception as e:
        logger.debug(f"Colorado SODA API failed: {e}")
    return None


# ── Florida Sunbiz: try cloudscraper, fallback to DDG ────────────────

@register_state("florida")
def _search_florida(company_name: str) -> dict | None:
    """Florida Sunbiz — try direct with cloudscraper, then DDG fallback."""
    # Try cloudscraper if available (handles Cloudflare)
    try:
        import cloudscraper
        scraper = cloudscraper.create_scraper()

        clean = company_name.upper().replace(",", "").replace(".", "")
        search_url = (
            "https://search.sunbiz.org/Inquiry/CorporationSearch/SearchResults"
            f"?inquiryType=EntityName&searchTerm={quote_plus(clean)}"
            f"&searchNameOrder={quote_plus(clean.replace(' ', ''))}"
        )
        resp = scraper.get(search_url, timeout=15)
        if resp.status_code == 200 and "SearchResultDetail" in resp.text:
            soup = BeautifulSoup(resp.text, "lxml")
            # Find first detail link
            for a in soup.select("a[href*='SearchResultDetail']"):
                detail_url = urljoin("https://search.sunbiz.org", a["href"])
                detail_resp = scraper.get(detail_url, timeout=15)
                if detail_resp.status_code == 200:
                    result = _parse_sunbiz_detail(detail_resp.text, company_name)
                    if result:
                        return result
                break  # only try first result
    except ImportError:
        pass  # cloudscraper not installed
    except Exception as e:
        logger.debug(f"Sunbiz direct failed: {e}")

    # Fallback: DDG search
    return _search_via_ddg(company_name, "sunbiz.org", "florida")


def _parse_sunbiz_detail(html: str, company_name: str = "") -> dict | None:
    """Parse Sunbiz detail page for officer names."""
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text(" ", strip=True)

    # Look for officer section patterns
    # Sunbiz lists: Title, Name, Address in sequential table rows
    officer_patterns = [
        # "Title MGR Name JOHN DOE"
        r"Title\s+(MGR|CEO|PRESIDENT|MANAGER|MEMBER|OWNER|MANAGING MEMBER|DIRECTOR)\s+Name\s+([A-Z][A-Z\s,]+?)(?:\s+Address|\s+\d)",
        # "Officer/Director Detail ... Name ... Address"
        r"(?:Officer|Director|Manager|Member).*?Name\s+([A-Z][A-Z\s,]+?)(?:\s+Address|\s+\d)",
    ]
    for pattern in officer_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            # Get the name group (last capture group)
            raw_name = match.group(match.lastindex).strip()
            # Sunbiz often uses "LAST, FIRST" format
            if "," in raw_name:
                parts = [p.strip() for p in raw_name.split(",")]
                if len(parts) == 2:
                    raw_name = f"{parts[1]} {parts[0]}"
            name = raw_name.title()
            if _is_valid_person_name(name, company_name):
                return {"name": name, "title": "Officer", "source": "sunbiz"}
    return None


# ── Texas: Comptroller API (free, documented) ────────────────────────
# NOTE: Requires free API key from api-doc.comptroller.texas.gov
# Set TEXAS_COMPTROLLER_API_KEY in .env to enable

@register_state("texas")
def _search_texas(company_name: str) -> dict | None:
    """Texas Comptroller API for franchise tax records with officer names."""
    import os
    api_key = os.getenv("TEXAS_COMPTROLLER_API_KEY", "")
    if not api_key:
        # No API key, fall back to DDG
        return _search_via_ddg(company_name, "sos.state.tx.us", "texas")

    try:
        search_url = "https://api.comptroller.texas.gov/public-data/v1/public/franchise-tax-list"
        resp = requests.get(
            search_url,
            params={"name": company_name},
            headers={"x-api-key": api_key, **HEADERS},
            timeout=10,
        )
        if resp.status_code != 200:
            return _search_via_ddg(company_name, "sos.state.tx.us", "texas")

        results = resp.json()
        if not results:
            return None

        # Get detail for first result
        taxpayer_id = results[0].get("taxpayerId")
        if not taxpayer_id:
            return None

        detail_resp = requests.get(
            f"https://api.comptroller.texas.gov/public-data/v1/public/franchise-tax/{taxpayer_id}",
            headers={"x-api-key": api_key, **HEADERS},
            timeout=10,
        )
        if detail_resp.status_code != 200:
            return None

        detail = detail_resp.json()
        # Look for officer fields
        for field in ("officerName", "officer_name", "responsiblePartyName", "managerName"):
            name = detail.get(field, "")
            if name and _is_valid_person_name(name.title(), company_name):
                return {"name": name.title(), "title": "Officer", "source": "texas_comptroller"}

    except Exception as e:
        logger.debug(f"Texas Comptroller API failed: {e}")

    return _search_via_ddg(company_name, "sos.state.tx.us", "texas")


# ── All other states: DDG site-search fallback ────────────────────────

_STATE_SOS_DOMAINS = {
    "california": "bizfileonline.sos.ca.gov",
    "arizona": "ecorp.azcc.gov",
    "georgia": "ecorp.sos.ga.gov",
    "north_carolina": "sosnc.gov",
    "nevada": "nvsos.gov",
    "tennessee": "sos.tn.gov",
    "ohio": "sos.state.oh.us",
    "virginia": "scc.virginia.gov",
    "south_carolina": "sos.sc.gov",
    "alabama": "sos.alabama.gov",
    "louisiana": "sos.la.gov",
    "maryland": "dat.maryland.gov",
    "indiana": "inbiz.in.gov",
    "missouri": "sos.mo.gov",
    "michigan": "cofs.lara.state.mi.us",
    "pennsylvania": "dos.pa.gov",
    "new_jersey": "njportal.com",
}

# Register all DDG-fallback states
for _state, _domain in _STATE_SOS_DOMAINS.items():
    def _make_scraper(domain, state):
        def scraper(company_name):
            return _search_via_ddg(company_name, domain, state)
        return scraper
    _STATE_SCRAPERS[_state] = _make_scraper(_domain, _state)


def _search_via_ddg(company_name: str, state_domain: str, state_name: str) -> dict | None:
    """DuckDuckGo site-search for state SOS records."""
    try:
        query = f'site:{state_domain} "{company_name}" officer OR agent OR manager OR member'
        resp = requests.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            headers=HEADERS,
            timeout=10,
        )
        if resp.status_code != 200:
            return None

        soup = BeautifulSoup(resp.text, "lxml")
        snippets = " ".join(r.get_text(" ", strip=True) for r in soup.select(".result__snippet"))

        if snippets:
            owner = _extract_owner_from_text(snippets, company_name)
            if owner:
                owner["source"] = f"{state_name}_sos_search"
                return owner
    except Exception as e:
        logger.debug(f"{state_name} SOS search failed for {company_name}: {e}")
    return None


# ══════════════════════════════════════════════════════════════════════
#  GOOGLE → LINKEDIN MINING
# ══════════════════════════════════════════════════════════════════════

def find_owner_via_google_linkedin(company_name: str, city: str = "", state: str = "") -> dict | None:
    """
    Google x-ray search for LinkedIn profiles of business owners.
    Query: site:linkedin.com/in ("owner" OR "founder") "Company Name" "City"
    """
    # Build query
    parts = [f'site:linkedin.com/in ("owner" OR "founder" OR "president" OR "CEO")']
    parts.append(f'"{company_name}"')
    if city:
        parts.append(f'"{city}"')
    query = " ".join(parts)

    try:
        # Use Google via DuckDuckGo (Google direct blocks bots)
        resp = requests.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            headers=HEADERS,
            timeout=10,
        )
        if resp.status_code != 200:
            return None

        soup = BeautifulSoup(resp.text, "lxml")

        for result in soup.select(".result"):
            title = result.select_one(".result__title")
            snippet = result.select_one(".result__snippet")
            if not title:
                continue

            title_text = title.get_text(" ", strip=True)
            snippet_text = snippet.get_text(" ", strip=True) if snippet else ""

            # LinkedIn titles are like "John Smith - Owner - Company Name | LinkedIn"
            name = _extract_name_from_linkedin_title(title_text, company_name)
            if name:
                # Try to get title from snippet
                person_title = "Owner"
                for t in ("Owner", "Founder", "CEO", "President", "Principal"):
                    if t.lower() in (title_text + " " + snippet_text).lower():
                        person_title = t
                        break
                return {"name": name, "title": person_title, "source": "linkedin_google"}

    except Exception as e:
        logger.debug(f"Google/LinkedIn search failed for {company_name}: {e}")
    return None


def _extract_name_from_linkedin_title(title: str, company_name: str) -> str | None:
    """
    Extract person name from LinkedIn result title.
    Format: "John Smith - Owner at Company | LinkedIn"
    or: "John Smith | LinkedIn"
    """
    # Remove " | LinkedIn" or "- LinkedIn"
    cleaned = re.sub(r"\s*[\-|]\s*LinkedIn.*$", "", title, flags=re.IGNORECASE).strip()
    # Take first segment before " - " (the name part)
    name = cleaned.split(" - ")[0].strip()
    # Remove trailing titles
    name = re.sub(r"\s*[,\-]\s*(Owner|Founder|CEO|President|Principal|Director).*$", "", name, flags=re.IGNORECASE).strip()

    if _is_valid_person_name(name, company_name):
        return name
    return None


# ══════════════════════════════════════════════════════════════════════
#  GENERIC SEARCH FALLBACK
# ══════════════════════════════════════════════════════════════════════

def find_owner_via_search(company_name: str, city: str = "", state: str = "") -> dict | None:
    """DuckDuckGo general search for owner/founder info."""
    location = f"{city} {state}".strip() if city or state else ""
    query = f'"{company_name}" {location} owner OR founder OR CEO'.strip()

    try:
        resp = requests.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            headers=HEADERS,
            timeout=10,
        )
        if resp.status_code != 200:
            return None

        soup = BeautifulSoup(resp.text, "lxml")
        snippets = " ".join(r.get_text(" ", strip=True) for r in soup.select(".result__snippet"))
        titles = " ".join(r.get_text(" ", strip=True) for r in soup.select(".result__title"))

        full_text = snippets + " " + titles
        return _extract_owner_from_text(full_text, company_name)
    except Exception as e:
        logger.debug(f"DDG search failed for {company_name}: {e}")
    return None


def _extract_owner_from_text(text: str, company_name: str) -> dict | None:
    """Extract owner name from search result text."""
    patterns = [
        r"(?:owned|founded|started|led|run)\s+by\s+([A-Z][a-z]+\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)",
        r"(?:owner|founder|ceo|president|principal)[:\s,]+([A-Z][a-z]+\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)",
        r"([A-Z][a-z]+\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)[,\s]+(?:owner|founder|ceo|president|principal)",
        r"([A-Z][a-z]+\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\s+is\s+(?:the\s+)?(?:owner|founder|ceo)",
    ]

    for pattern in patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            name = match.group(1).strip()
            if _is_valid_person_name(name, company_name):
                return {"name": name, "title": "Owner", "source": "search"}
    return None
