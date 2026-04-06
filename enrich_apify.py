"""Enrich leads using Apify Decision Maker Name & Email Extractor."""
import json, os, sys
sys.path.insert(0, os.path.dirname(__file__))

from apify_client import ApifyClient
from dotenv import load_dotenv
load_dotenv()

client = ApifyClient(os.getenv("APIFY_API_KEY"))
ACTOR_ID = "dominic-quaiser/decision-maker-name-email-extractor"

with open("data/epoxy_fl_500.json") as f:
    leads = json.load(f)

urls = [{"url": l["website"] if l["website"].startswith("http") else "https://" + l["website"]} for l in leads if l.get("website")]
print(f"Sending {len(urls)} websites to Apify decision maker extractor...")

# Run in batches of 50 to avoid timeouts
BATCH = 50
all_results = []

for i in range(0, len(urls), BATCH):
    batch = urls[i:i+BATCH]
    print(f"Batch {i//BATCH+1}/{(len(urls)-1)//BATCH+1} ({len(batch)} sites)...", flush=True)

    run_input = {
        "startUrls": batch,
        "jobTitles": ["Founders & ownership", "C\u2011Suite", "Presidents & directors"],
        "explorationMode": "restricted",
        "emailSearchMode": "thorough",
        "depth": 2,
        "pagecount": 30,
        "proxyConfiguration": {"useApifyProxy": True},
    }

    try:
        run = client.actor(ACTOR_ID).call(run_input=run_input, timeout_secs=600)
        items = client.dataset(run["defaultDatasetId"]).list_items().items
        all_results.extend(items)
        print(f"  Got {len(items)} results this batch", flush=True)
    except Exception as e:
        print(f"  Batch failed: {e}", flush=True)

print(f"\nTotal enrichment results: {len(all_results)}")

# Save raw enrichment data
with open("data/apify_enrichment_raw.json", "w") as f:
    json.dump(all_results, f, indent=2)

# Merge back into leads
url_to_enrichment = {}
for item in all_results:
    url = (item.get("url") or item.get("website") or item.get("source") or "").rstrip("/").lower()
    if url and not url_to_enrichment.get(url):
        url_to_enrichment[url] = item

merged = 0
for lead in leads:
    w = lead["website"].rstrip("/").lower()
    if not w.startswith("http"):
        w = "https://" + w
    match = url_to_enrichment.get(w) or url_to_enrichment.get(w.replace("https://", "http://"))
    if not match:
        # try without www
        for key, val in url_to_enrichment.items():
            if w.replace("www.", "") in key or key.replace("www.", "") in w:
                match = val
                break
    if match:
        name = match.get("name") or match.get("contactName") or match.get("decisionMakerName") or ""
        title = match.get("title") or match.get("jobTitle") or match.get("position") or ""
        email = match.get("email") or match.get("contactEmail") or ""
        if name:
            lead["decision_maker_name"] = name
            lead["decision_maker_title"] = title
        if email:
            lead["email"] = email
            lead["email_type"] = "decision_maker" if name else "generic"
            lead["email_confidence"] = "high"
        if name or email:
            merged += 1

print(f"Merged enrichment into {merged}/{len(leads)} leads")
dm_count = sum(1 for l in leads if l.get("decision_maker_name"))
email_count = sum(1 for l in leads if l.get("email"))
print(f"Final: {dm_count} decision makers, {email_count} emails out of {len(leads)} leads")

with open("data/epoxy_fl_enriched_v2.json", "w") as f:
    json.dump(leads, f, indent=2)
print("Saved to data/epoxy_fl_enriched_v2.json")
