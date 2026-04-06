"""
Generate personalized cold email outreach based on Facebook ad intelligence.

Follows the Bookedly cold email playbook (see bookedly_context.md):
- Under 100 words body, ideally under 75
- Hook from ad intelligence or Situation A/B/C
- One proof point matched to their industry
- One low-friction ask
- Sign off as Yaniv
- No dashes, no corporate language, no exclamation points
"""
from __future__ import annotations

import json
import logging
import re

import config

logger = logging.getLogger(__name__)

# ── Proof points by niche (from Bookedly context) ──────────────────────

PROOF = {
    "roofing": {
        "case_1": "A roofer in Texas went from 30 jobs a month to 85 in 90 days. No shared leads.",
        "case_2": "A Florida roofer went from 12 full replacements to 28 in 90 days. At $14,000 average ticket that is an extra $200,000 a month.",
        "ticket": "$14,000",
    },
    "hvac": {
        "case_1": "An HVAC company went from 8 system replacements a month to 31 in 90 days. Same market, same crew, different positioning.",
        "case_2": "An HVAC company went from 8 replacements to 31 in 90 days. Same zip codes, same budget, different system behind the ad.",
        "ticket": "$8,000",
    },
    "solar": {
        "case_1": "A solar installer went from 22 jobs a month to 41. All inbound. No door knocking. No shared leads.",
        "case_2": "A solar company doing $22,000 average tickets went from 22 to 41 installs a month. Roughly $420,000 in additional monthly revenue from the same territory.",
        "ticket": "$22,000",
    },
    "remodeling": {
        "case_1": "A remodeling company generated $780,000 in signed contracts from homeowners who found them first and stopped looking.",
        "case_2": "A remodeling company generated $780,000 in signed contracts. All inbound. No shared leads.",
        "ticket": "$15,000",
    },
    "painting": {
        "case_1": "A roofer in Texas went from 30 jobs a month to 85 in 90 days. No shared leads.",
        "case_2": "A contractor went from 30 jobs to 85 in 90 days. Same market. Different system.",
        "ticket": "$5,000",
    },
    "epoxy": {
        "case_1": "A contractor went from 30 jobs a month to 85 in 90 days. No shared leads.",
        "case_2": "A contractor went from 30 jobs to 85 in 90 days. Same market. Different system.",
        "ticket": "$5,000",
    },
}

DEFAULT_PROOF = {
    "case_1": "A contractor in your space went from 30 jobs a month to 85 in 90 days. No shared leads.",
    "case_2": "A contractor went from 30 jobs to 85 in 90 days. Same market. Different system.",
    "ticket": "$10,000",
}

# ── Low-friction asks (rotate per lead to avoid repetition) ────────────

ASKS = [
    "Is controlling your pipeline something you're working on or not a priority right now?",
    "Worth a quick look at what your market is actually doing?",
    "Is that the kind of thing you're actively working on right now?",
    "Would it be useful to see what that looks like for your market specifically?",
]

ASKS_NO_ADS = [
    "Is controlling your pipeline something you're working on or not a priority right now?",
    "Worth a quick conversation or not your priority right now?",
    "Would it be okay if I sent you a short breakdown of what we did for a {niche} company in your state?",
]


def _extract_first_name(dm_name: str) -> str:
    """
    Extract a valid first name from decision_maker_name.
    Returns empty string if the name looks like scraped garbage.
    """
    if not dm_name or not dm_name.strip():
        return ""

    parts = dm_name.strip().split()

    # Must be 2-3 words (first + last, maybe middle)
    if len(parts) < 2 or len(parts) > 4:
        return ""

    first = parts[0]

    # Must be alphabetic, reasonable length, capitalized like a name
    if not first.isalpha() or len(first) < 2 or len(first) > 15:
        return ""

    # Reject common non-name words that scrapers pull
    junk_words = {
        "the", "and", "our", "get", "see", "new", "top", "best", "free",
        "call", "book", "just", "had", "has", "was", "are", "for", "not",
        "all", "well", "run", "flow", "roof", "home", "air", "more",
        "learn", "read", "click", "view", "check", "find", "start",
        "your", "this", "that", "with", "from", "about", "been",
    }
    if first.lower() in junk_words:
        return ""

    # Should start with uppercase
    if not first[0].isupper():
        return ""

    return first


def _get_proof(niche: str) -> dict:
    """Get proof points for a niche, falling back to default."""
    key = (niche or "").lower().strip()
    for k, v in PROOF.items():
        if k in key:
            return v
    return DEFAULT_PROOF


def _niche_label(niche: str) -> str:
    """Clean niche label for use in copy. Returns a usable trade name like 'roofing'."""
    n = (niche or "").lower().strip()
    if not n or n == "none":
        return "roofing"  # default for now since we're targeting roofers

    # Map common niche strings to clean labels
    niche_map = {
        "roofing": "roofing",
        "roof": "roofing",
        "roofer": "roofing",
        "hvac": "HVAC",
        "solar": "solar",
        "painting": "painting",
        "painter": "painting",
        "remodel": "remodeling",
        "kitchen": "remodeling",
        "epoxy": "epoxy",
        "plumb": "plumbing",
        "electric": "electrical",
    }
    for key, label in niche_map.items():
        if key in n:
            return label

    # Remove "company", "contractor" etc
    for suffix in ["company", "contractor", "services", "service"]:
        n = n.replace(suffix, "").strip()
    return n or "home services"


def _pick_ask(lead_id: str | int, asks: list[str], **fmt) -> str:
    """Deterministically pick an ask based on lead ID to avoid everyone getting the same one."""
    idx = hash(str(lead_id)) % len(asks)
    return asks[idx].format(**fmt)


# ── LLM-powered ad creative analysis ──────────────────────────────────

def _analyze_creative_llm(samples: list, niche: str) -> dict | None:
    """
    Use Claude Haiku to analyze ad creative and return a specific observation.

    Returns dict with:
        - observation: one specific sentence about their ads (for the email hook)
        - creative_type: 'price_led' | 'offer_led' | 'trust_led' | 'weak' | 'generic'
        - weakness: what's wrong with the creative (internal use)
    Returns None if API unavailable or fails.
    """
    if not config.ANTHROPIC_API_KEY:
        return None

    # Build ad text block from samples
    ad_texts = []
    for i, s in enumerate(samples[:5], 1):
        if isinstance(s, dict):
            parts = []
            if s.get("text"):
                parts.append(s["text"])
            if s.get("title"):
                parts.append(f"Headline: {s['title']}")
            if s.get("cta_text"):
                parts.append(f"CTA: {s['cta_text']}")
            if s.get("link_url"):
                parts.append(f"Links to: {s['link_url']}")
            if parts:
                ad_texts.append(f"Ad {i}:\n" + "\n".join(parts))
        elif isinstance(s, str) and s.strip():
            ad_texts.append(f"Ad {i}:\n{s}")

    if not ad_texts:
        return None

    ads_block = "\n\n".join(ad_texts)

    prompt = f"""You are analyzing Facebook ads for a {niche} company. Based on the ads below, respond with ONLY a JSON object (no markdown, no explanation).

ADS:
{ads_block}

Respond with this exact JSON structure:
{{"observation": "<one specific sentence about what you noticed in their ads — mention what the ad is doing wrong or what it leads with. Write as if telling the business owner: 'You are running...' or 'Your ads lead with...' Keep under 20 words. No dashes. No exclamation points.>", "creative_type": "<one of: price_led, offer_led, trust_led, weak, generic>", "weakness": "<one sentence: what is the main problem with this creative from a lead quality perspective>"}}

Rules for creative_type:
- price_led: ads lead with discounts, free estimates, lowest price, financing offers
- offer_led: ads push a specific promo or limited deal but not purely price
- trust_led: ads lead with credentials, years of experience, warranties, reviews
- weak: ads are vague, generic, no clear hook, no differentiation, or just boosted posts
- generic: decent ads that don't fit the above categories

Rules for observation:
- Be specific about what you actually see in the ad copy
- Reference the actual offer, angle, or language they use
- Do NOT be complimentary. This is for a cold email that points out a problem.
- Examples: "Your ads are offering free estimates to get clicks" or "You are running the same before and after creative with no clear offer" or "Your creative leads with price and financing"
"""

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()

        # Parse JSON — handle potential markdown wrapping
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()

        result = json.loads(text)

        # Validate required fields
        if "observation" in result and "creative_type" in result:
            # Clean observation — no dashes, no exclamation marks
            obs = result["observation"].replace("—", "").replace("–", "").replace("!", ".").strip()
            result["observation"] = obs
            return result

    except Exception as e:
        logger.warning(f"LLM creative analysis failed: {e}")

    return None


# ── Keyword-based fallback classification ──────────────────────────────

def _classify_ad_creative(ad_texts: list[str]) -> str:
    """
    Fallback: classify ad creative type from text with keyword matching.
    Returns: 'price_led', 'trust_led', or 'generic'
    """
    if not ad_texts:
        return "generic"

    combined = " ".join(ad_texts).lower()

    price_signals = [
        "free estimate", "free quote", "free inspection",
        "% off", "$ off", "discount", "special offer",
        "best price", "lowest price", "affordable",
        "no cost", "complimentary", "save $", "starting at $",
        "financing", "0% interest", "no money down",
    ]
    trust_signals = [
        "years of experience", "family owned", "licensed",
        "certified", "trusted", "guaranteed", "warranty",
        "5 star", "top rated", "award", "a+ rating",
    ]

    price_count = sum(1 for s in price_signals if s in combined)
    trust_count = sum(1 for s in trust_signals if s in combined)

    if price_count >= 2 or (price_count >= 1 and trust_count == 0):
        return "price_led"
    if trust_count >= 2:
        return "trust_led"
    return "generic"


# ── Structural ad intelligence (links, dates, variety) ─────────────────

def _analyze_ad_intel(samples: list, lead_website: str, niche: str) -> dict:
    """
    Analyze structured ad data for deeper intelligence.
    Combines LLM analysis (if available) with structural signals.
    """
    from datetime import datetime, timezone

    intel = {
        "creative_type": "generic",
        "sends_to_homepage": False,
        "is_stale": False,
        "stale_months": 0,
        "unique_creatives": 0,
        "llm_observation": "",
        "weakness": "",
    }

    if not samples:
        return intel

    # ── LLM analysis (best signal — uses actual ad copy understanding) ──
    llm_result = _analyze_creative_llm(samples, niche)
    if llm_result:
        intel["creative_type"] = llm_result.get("creative_type", "generic")
        intel["llm_observation"] = llm_result.get("observation", "")
        intel["weakness"] = llm_result.get("weakness", "")
    else:
        # Fallback to keyword matching
        all_texts = []
        for s in samples:
            if isinstance(s, dict):
                for field in ("text", "title", "cta_text"):
                    val = s.get(field, "")
                    if val:
                        all_texts.append(val)
            elif isinstance(s, str) and s.strip():
                all_texts.append(s)
        intel["creative_type"] = _classify_ad_creative(all_texts)

    # ── Count unique creatives ──
    unique_bodies = set()
    for s in samples:
        if isinstance(s, dict):
            text = s.get("text", "")
            if text:
                unique_bodies.add(text[:100])
        elif isinstance(s, str) and s.strip():
            unique_bodies.add(s[:100])
    intel["unique_creatives"] = len(unique_bodies)

    # ── Check link destination ──
    # Skip homepage check if no link_url at all (likely instant form ads)
    clean_domain = _clean_domain(lead_website)
    has_any_link = False
    for s in samples:
        if not isinstance(s, dict):
            continue
        link = (s.get("link_url") or "").strip().rstrip("/")
        if not link:
            continue
        # Ignore Facebook internal URLs (instant forms, messenger, etc)
        link_lower = link.lower()
        if "facebook.com" in link_lower or "fb.com" in link_lower or "m.me" in link_lower:
            continue
        has_any_link = True
        link_path = _extract_path(link)
        # Homepage = root path or empty path
        if link_path in ("", "/", "/index.html", "/index.php"):
            intel["sends_to_homepage"] = True
            break
        # Their own domain with a shallow generic path
        link_domain = _clean_domain(link)
        if clean_domain and link_domain == clean_domain:
            if link_path in ("/contact", "/about", "/services", "/contact-us"):
                intel["sends_to_homepage"] = True
                break

    # ── Check staleness ──
    oldest = None
    for s in samples:
        if not isinstance(s, dict):
            continue
        start = s.get("start_date", "")
        if start and isinstance(start, str) and len(start) >= 10:
            try:
                dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
                if oldest is None or dt < oldest:
                    oldest = dt
            except (ValueError, TypeError):
                pass

    if oldest:
        now = datetime.now(timezone.utc)
        days_running = (now - oldest).days
        intel["is_stale"] = days_running >= 90
        intel["stale_months"] = max(1, days_running // 30)

    return intel


def _clean_domain(url: str) -> str:
    """Extract bare domain from a URL. 'https://www.example.com/page' → 'example.com'"""
    if not url:
        return ""
    url = url.lower().strip()
    for prefix in ("https://", "http://", "www."):
        url = url.removeprefix(prefix)
    return url.split("/")[0].split("?")[0]


def _extract_path(url: str) -> str:
    """Extract path from URL. 'https://example.com/landing' → '/landing'"""
    if not url:
        return ""
    url = url.strip()
    for prefix in ("https://", "http://"):
        url = url.removeprefix(prefix)
    parts = url.split("/", 1)
    return "/" + parts[1].rstrip("/") if len(parts) > 1 else ""


# ── Main entry point ──────────────────────────────────────────────────

def generate_angle(lead: dict, ad_data: dict) -> dict:
    """
    Generate a personalized cold email (Email 1) based on Facebook ad intelligence.

    Returns dict with:
        - subject_line: email subject
        - outreach_angle: the full email body (under 100 words)
        - angle_type: "has_ads_price" | "has_ads_homepage" | "has_ads_stale" | "has_ads_generic" | "no_ads" | etc.
        - ad_analysis: short summary for internal use
    """
    has_ads = ad_data.get("has_ads")
    ad_count = ad_data.get("ad_count", 0)
    samples = _parse_samples(ad_data.get("ad_samples", "[]"))

    name = lead.get("name") or "your company"
    city = (lead.get("city") or "").split(",")[0].strip() or "your area"
    state = lead.get("state") or ""
    niche = lead.get("niche") or ""
    rating = lead.get("rating") or 0
    review_count = lead.get("review_count") or 0
    lead_id = lead.get("id") or lead.get("place_id") or name
    dm_name = lead.get("decision_maker_name") or ""
    website = lead.get("website") or ""

    # First name for greeting — only use if it looks like a real person name
    first_name = _extract_first_name(dm_name)
    greeting = f"Hey {first_name}," if first_name else "Hey,"

    # Infer niche from category or company name if niche field is empty
    effective_niche = niche
    if not effective_niche or effective_niche.lower() == "none":
        effective_niche = lead.get("category") or name or "home services"
    proof = _get_proof(effective_niche)
    niche_label = _niche_label(effective_niche)

    # Ad check failed or not done
    if has_ads is None:
        return {
            "subject_line": "",
            "outreach_angle": "",
            "angle_type": "",
            "ad_analysis": "Ad check pending",
        }

    if has_ads and ad_count > 0:
        intel = _analyze_ad_intel(samples, website, niche_label)
        return _angle_has_ads(
            name, city, state, niche_label, ad_count, intel,
            proof, greeting, lead_id,
        )
    else:
        return _angle_no_ads(
            name, city, state, niche_label, rating, review_count,
            proof, greeting, lead_id,
        )


# ── Angle generators ──────────────────────────────────────────────────

def _angle_has_ads(
    name: str, city: str, state: str, niche: str,
    ad_count: int, intel: dict,
    proof: dict, greeting: str, lead_id,
) -> dict:
    """
    They're running ads (Situation B or C).
    Pick the best angle based on real ad intelligence.

    Priority:
    1. LLM gave a specific observation → use it as the hook
    2. Price-led → wrong homeowners angle
    3. Sends to homepage → broken funnel angle
    4. Stale creative → ad fatigue angle
    5. Generic → post-click system angle
    """
    creative_type = intel.get("creative_type", "generic")
    sends_to_homepage = intel.get("sends_to_homepage", False)
    is_stale = intel.get("is_stale", False)
    stale_months = intel.get("stale_months", 0)
    unique_creatives = intel.get("unique_creatives", 0)
    llm_obs = intel.get("llm_observation", "")

    ask = _pick_ask(lead_id, ASKS)

    if creative_type in ("price_led", "offer_led"):
        subject = "Your Facebook ads"
        if llm_obs:
            hook = f"Looked up your ads on Meta. {llm_obs}"
        else:
            hook = "Looked up your ads on Meta. You're running creative that leads with price."
        pain = (
            f"The homeowners clicking on discount offers are not the ones spending "
            f"{proof['ticket']} on a full job. You are attracting the wrong conversation from the start."
        )
        body = f"{greeting}\n\n{hook}\n\n{pain}\n\n{proof['case_2']}\n\n{ask}\n\nYaniv\nBookedly"
        angle_type = "has_ads_price"

    elif sends_to_homepage:
        subject = f"{niche.title()} question for you"
        hook = f"Saw your ads running in {city}. They're sending traffic to your homepage."
        pain = (
            "Most homeowners who click land there, look around for 10 seconds, and leave. "
            "The ad did its job. The page after it didn't."
        )
        body = f"{greeting}\n\n{hook}\n\n{pain}\n\n{proof['case_2']}\n\n{ask}\n\nYaniv\nBookedly"
        angle_type = "has_ads_homepage"

    elif creative_type == "weak":
        # LLM detected weak/generic creative
        subject = "Your Facebook ads"
        if llm_obs:
            hook = f"Looked up your ads on Meta. {llm_obs}"
        else:
            hook = "Looked up your ads on Meta. The creative is not doing the heavy lifting it should."
        pain = (
            "A weak ad still gets some clicks. But the homeowners it attracts are not pre-sold. "
            "They are just browsing. That means more calls, more no-shows, more tire kickers."
        )
        body = f"{greeting}\n\n{hook}\n\n{pain}\n\n{proof['case_1']}\n\n{ask}\n\nYaniv\nBookedly"
        angle_type = "has_ads_weak"

    elif is_stale and unique_creatives <= 2:
        subject = "Your Facebook ads"
        if stale_months >= 3:
            hook = (
                f"Looked up your ads on Meta. You've been running "
                f"the same creative for about {stale_months} months."
            )
        else:
            hook = (
                "Looked up your ads on Meta. Looks like you've been running "
                "the same creative for a while."
            )
        pain = (
            "When the same homeowner sees the same ad for the sixth time, they stop noticing. "
            "The ad worked at first. Now it is just budget running."
        )
        body = f"{greeting}\n\n{hook}\n\n{pain}\n\n{proof['case_1']}\n\n{ask}\n\nYaniv\nBookedly"
        angle_type = "has_ads_stale"

    elif ad_count <= 3:
        subject = "Your Facebook ads"
        if llm_obs:
            hook = f"Looked up your ads on Meta. {llm_obs}"
        else:
            hook = (
                "Looked up your ads on Meta. Looks like you've been running "
                "the same creative for a while."
            )
        pain = (
            "When the same homeowner sees the same ad for the sixth time, they stop noticing. "
            "The ad worked at first. Now it is just budget running."
        )
        body = f"{greeting}\n\n{hook}\n\n{pain}\n\n{proof['case_1']}\n\n{ask}\n\nYaniv\nBookedly"
        angle_type = "has_ads_stale"

    else:
        # Multiple varied ads — more sophisticated advertiser
        subject = f"{niche.title()} question for you"
        if llm_obs:
            hook = f"Saw your ads running in {city}. {llm_obs}"
        else:
            hook = f"Saw your ads running in {city}."
        pain = (
            "Most of the time the ad does its job. "
            "What breaks is what happens after someone clicks. "
            "No system means the lead goes cold before anyone calls."
        )
        body = f"{greeting}\n\n{hook}\n\n{pain}\n\n{proof['case_1']}\n\n{ask}\n\nYaniv\nBookedly"
        angle_type = "has_ads_generic"

    # Internal tracking
    parts = [f"{ad_count} ad(s)"]
    parts.append(f"creative: {creative_type}")
    if sends_to_homepage:
        parts.append("homepage traffic")
    if is_stale:
        parts.append(f"stale ({stale_months}mo)")
    parts.append(f"{unique_creatives} unique")
    if llm_obs:
        parts.append(f"LLM: {llm_obs[:60]}")
    analysis = " | ".join(parts)

    return {
        "subject_line": subject,
        "outreach_angle": body,
        "angle_type": angle_type,
        "ad_analysis": analysis,
    }


def _angle_no_ads(
    name: str, city: str, state: str, niche: str,
    rating: float, review_count: int,
    proof: dict, greeting: str, lead_id,
) -> dict:
    """
    No ads running (Situation A).
    Write to the referral trap and seasonal dependency.
    """
    ask = _pick_ask(lead_id, ASKS_NO_ADS, niche=niche)

    if rating >= 4.0 and review_count >= 20:
        subject = f"{niche.title()} pipeline in {city}"
        hook = (
            f"Checked the Meta library. No ads running for {name} right now."
        )
        pain = (
            f"You have {rating} stars and {review_count} reviews. That reputation converts. "
            f"But homeowners in {city} searching on Facebook are finding your competitors instead "
            f"because they are running ads and you are not."
        )
        body = f"{greeting}\n\n{hook}\n\n{pain}\n\n{proof['case_1']}\n\n{ask}\n\nYaniv\nBookedly"
        angle_type = "no_ads_strong_reviews"

    else:
        subject = f"{niche.title()} pipeline in {city}"
        hook = (
            "Checked the Meta library. No ads running for your company right now."
        )
        pain = (
            "That usually means the calendar depends on referrals and whatever "
            "season brings in. Works until it doesn't."
        )
        body = f"{greeting}\n\n{hook}\n\n{pain}\n\n{proof['case_1']}\n\n{ask}\n\nYaniv\nBookedly"
        angle_type = "no_ads_referral"

    analysis = f"No active Facebook ads. Rating: {rating}, Reviews: {review_count}"

    return {
        "subject_line": subject,
        "outreach_angle": body,
        "angle_type": angle_type,
        "ad_analysis": analysis,
    }


# ── Helpers ────────────────────────────────────────────────────────────

def _parse_samples(samples_json: str) -> list:
    """
    Parse ad samples from JSON string.
    Handles both old format (list of strings) and new format (list of dicts).
    """
    try:
        parsed = json.loads(samples_json) if samples_json else []
        if not isinstance(parsed, list):
            return []
        result = []
        for item in parsed:
            if isinstance(item, dict) and (item.get("text") or item.get("title")):
                result.append(item)
            elif isinstance(item, str) and item.strip():
                result.append(item)
        return result
    except (json.JSONDecodeError, TypeError):
        return []
