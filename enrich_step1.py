"""Step 1: Enrich 405 epoxy leads with decision makers + emails using website scraping."""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(__file__))

from enrichment.website_scraper import find_decision_makers
from enrichment.email_finder import find_email

with open("data/epoxy_fl_500.json") as f:
    leads = json.load(f)

print(f"Enriching {len(leads)} leads...")

dm_found = 0
email_found = 0

for i, lead in enumerate(leads):
    website = lead.get("website", "")
    if not website:
        continue

    # Find decision maker
    try:
        dms = find_decision_makers(website)
        if dms:
            lead["decision_maker_name"] = dms[0]["name"]
            lead["decision_maker_title"] = dms[0]["title"]
            dm_found += 1
    except Exception:
        pass

    # Find email
    try:
        person_name = lead.get("decision_maker_name")
        email_result = find_email(website, person_name)
        if email_result.get("email"):
            lead["email"] = email_result["email"]
            lead["email_type"] = email_result.get("email_type", "")
            lead["email_confidence"] = email_result.get("confidence", "")
            email_found += 1
    except Exception:
        pass

    if (i + 1) % 20 == 0:
        print(f"  {i+1}/{len(leads)} done | DMs: {dm_found} | Emails: {email_found}", flush=True)

    time.sleep(0.3)

print(f"\nDone! DMs: {dm_found}/{len(leads)} ({dm_found*100//len(leads)}%) | Emails: {email_found}/{len(leads)} ({email_found*100//len(leads)}%)")

with open("data/epoxy_fl_enriched.json", "w") as f:
    json.dump(leads, f, indent=2)
print("Saved to data/epoxy_fl_enriched.json")
