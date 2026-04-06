"""Quick script: scrape ~500 epoxy flooring companies across Florida."""
import json, os, sys
sys.path.insert(0, os.path.dirname(__file__))

from apify_client import ApifyClient
from dotenv import load_dotenv
load_dotenv()

client = ApifyClient(os.getenv("APIFY_API_KEY"))

cities = [
    "Miami, FL", "Fort Lauderdale, FL", "West Palm Beach, FL",
    "Tampa, FL", "St Petersburg, FL", "Orlando, FL",
    "Jacksonville, FL", "Naples, FL", "Fort Myers, FL",
    "Sarasota, FL", "Daytona Beach, FL", "Gainesville, FL",
    "Tallahassee, FL", "Pensacola, FL", "Ocala, FL",
    "Lakeland, FL", "Port St Lucie, FL", "Cape Coral, FL",
    "Boca Raton, FL", "Clearwater, FL",
]

queries = [f"epoxy flooring company in {c}" for c in cities]
queries += [f"epoxy coating contractor in {c}" for c in cities[:10]]  # top 10 cities get 2nd query

print(f"Running {len(queries)} queries...")

run_input = {
    "searchStringsArray": queries,
    "maxCrawledPlacesPerSearch": 30,
    "language": "en",
    "maxImages": 0,
    "maxReviews": 0,
    "onlyDataFromSearchPage": False,
    "skipClosedPlaces": True,
}

run = client.actor("compass/crawler-google-places").call(run_input=run_input, timeout_secs=900)
items = client.dataset(run["defaultDatasetId"]).list_items().items
print(f"Raw results: {len(items)}")

# Filter to actual epoxy companies
JUNK = {"home depot", "lowe's", "lowes", "walmart", "target", "menards", "ace hardware", "sherwin"}
results = []
seen = set()
for item in items:
    name = (item.get("title") or item.get("name") or "").strip()
    website = (item.get("website") or "").strip().rstrip("/")
    if not name or not website:
        continue
    if any(j in name.lower() for j in JUNK):
        continue
    key = website.lower()
    if key in seen:
        continue
    seen.add(key)
    results.append({
        "name": name,
        "phone": (item.get("phone") or "").strip(),
        "website": website,
        "address": (item.get("address") or "").strip(),
        "city": (item.get("city") or "").strip(),
        "state": (item.get("state") or "").strip(),
        "rating": item.get("totalScore") or 0,
        "review_count": item.get("reviewsCount") or 0,
        "category": (item.get("categoryName") or "").strip(),
        "place_id": item.get("placeId") or "",
    })

print(f"Filtered unique companies with websites: {len(results)}")

os.makedirs("data", exist_ok=True)
with open("data/epoxy_fl_500.json", "w") as f:
    json.dump(results, f, indent=2)
print("Saved to data/epoxy_fl_500.json")
