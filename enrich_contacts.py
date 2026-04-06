"""Enrich leads using Apify contact-info-scraper for emails + phones + socials."""
import json, os, sys
sys.path.insert(0, os.path.dirname(__file__))

from apify_client import ApifyClient
from dotenv import load_dotenv
load_dotenv()

client = ApifyClient(os.getenv("APIFY_API_KEY"))
ACTOR = "vdrmota/contact-info-scraper"

with open("data/epoxy_fl_500.json") as f:
    leads = json.load(f)

urls = []
for l in leads:
    w = l.get("website", "")
    if w:
        if not w.startswith("http"):
            w = "https://" + w
        urls.append({"url": w})

print(f"Enriching {len(urls)} websites for contact info...", flush=True)

BATCH = 50
all_results = []

for i in range(0, len(urls), BATCH):
    batch = urls[i:i+BATCH]
    print(f"Batch {i//BATCH+1}/{(len(urls)-1)//BATCH+1} ({len(batch)} sites)...", flush=True)
    try:
        run = client.actor(ACTOR).call(
            run_input={
                "startUrls": batch,
                "maxRequestsPerStartUrl": 3,
            },
            timeout_secs=300,
        )
        items = client.dataset(run["defaultDatasetId"]).list_items().items
        all_results.extend(items)
        print(f"  Got {len(items)} results", flush=True)
    except Exception as e:
        print(f"  Failed: {e}", flush=True)

print(f"\nTotal contact results: {len(all_results)}")

with open("data/contact_enrichment_raw.json", "w") as f:
    json.dump(all_results, f, indent=2)

# Build domain -> contact map
domain_map = {}
for item in all_results:
    domain = (item.get("domain") or "").lower()
    if domain:
        domain_map[domain] = item

# Merge into leads
email_count = 0
phone_count = 0
for lead in leads:
    w = lead.get("website", "").lower().rstrip("/")
    # Extract domain
    d = w.replace("https://", "").replace("http://", "").replace("www.", "").split("/")[0]
    match = domain_map.get(d)
    if not match:
        # try with www
        match = domain_map.get("www." + d)
    if not match:
        continue

    emails = match.get("emails", [])
    if emails and not lead.get("email"):
        # Prefer non-generic emails
        generic = {"info@","contact@","hello@","support@","help@","sales@","admin@","office@","service@","noreply@","no-reply@"}
        personal = [e for e in emails if not any(e.lower().startswith(g) for g in generic)]
        best = personal[0] if personal else emails[0]
        lead["email"] = best
        lead["email_type"] = "personal" if personal else "generic"
        lead["email_confidence"] = "high"
        email_count += 1

    phones = match.get("phones", [])
    if phones and not lead.get("phone"):
        lead["phone"] = phones[0]
        phone_count += 1

    # Add socials
    li = match.get("linkedIns", [])
    if li:
        lead["linkedin"] = li[0]
    fb = match.get("facebooks", [])
    if fb:
        lead["facebook"] = fb[0]
    ig = match.get("instagrams", [])
    if ig:
        lead["instagram"] = ig[0]

total_emails = sum(1 for l in leads if l.get("email"))
print(f"New emails added: {email_count}")
print(f"Total leads with email: {total_emails}/{len(leads)} ({total_emails*100//len(leads)}%)")

with open("data/epoxy_fl_enriched_v2.json", "w") as f:
    json.dump(leads, f, indent=2)
print("Saved to data/epoxy_fl_enriched_v2.json")
