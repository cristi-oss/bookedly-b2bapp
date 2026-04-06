"""
Test script for code_crafter/leads-finder Apify actor.
Apollo alternative for finding decision makers with emails.
"""
from __future__ import annotations

import json
from apify_client import ApifyClient
from dotenv import load_dotenv
import os

load_dotenv()

APIFY_API_KEY = os.getenv("APIFY_API_KEY")
ACTOR_ID = "code_crafter/leads-finder"

client = ApifyClient(APIFY_API_KEY)

# Test run — small batch: home service owners in Florida
run_input = {
    "numberOfLeads": 25,  # small test
    "runLabel": "test-epoxy-miami",
    "jobTitle": ["Owner", "Founder", "CEO", "President"],
    "location": ["Miami, Florida, United States"],
    "industry": ["Construction", "Building Materials"],
    "keywords": ["epoxy", "epoxy flooring", "epoxy coating"],
    "emailStatus": ["Verified"],  # only verified emails
}

print("Starting leads-finder actor...")
print(f"Input: {json.dumps(run_input, indent=2)}")
print()

try:
    run = client.actor(ACTOR_ID).call(
        run_input=run_input,
        timeout_secs=300,
    )

    print(f"Run finished. Status: {run.get('status')}")
    print(f"Dataset ID: {run.get('defaultDatasetId')}")
    print()

    # Fetch results
    items = client.dataset(run["defaultDatasetId"]).list_items().items

    print(f"Got {len(items)} results")
    print("=" * 80)

    for i, item in enumerate(items[:10], 1):
        print(f"\n--- Lead #{i} ---")
        print(f"  Name:     {item.get('name', 'N/A')}")
        print(f"  Title:    {item.get('title', item.get('jobTitle', 'N/A'))}")
        print(f"  Email:    {item.get('email', 'N/A')}")
        print(f"  Company:  {item.get('company', item.get('companyName', 'N/A'))}")
        print(f"  Website:  {item.get('website', item.get('companyWebsite', 'N/A'))}")
        print(f"  Location: {item.get('location', item.get('city', 'N/A'))}")
        print(f"  LinkedIn: {item.get('linkedinUrl', item.get('linkedin', 'N/A'))}")
        print(f"  Phone:    {item.get('phone', 'N/A')}")

    # Save full raw results for inspection
    if items:
        with open("data/leads_finder_test_raw.json", "w") as f:
            json.dump(items, f, indent=2)
        print(f"\nFull raw results saved to data/leads_finder_test_raw.json")

        # Show all keys from first result
        print(f"\nAll fields in first result:")
        for k, v in items[0].items():
            print(f"  {k}: {v}")

except Exception as e:
    print(f"Error: {e}")
    import traceback
    traceback.print_exc()
