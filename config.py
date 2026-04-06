import os
from datetime import date

from dotenv import load_dotenv

load_dotenv()

APIFY_API_KEY = os.getenv("APIFY_API_KEY")
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")
INSTANTLY_API_KEY = os.getenv("INSTANTLY_API_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

# --- Niches to target ---
NICHES = [
    "roofing company",
    "solar company",
    "solar installation",
    "HVAC company",
    "HVAC contractor",
    "epoxy flooring",
    "epoxy coating",
    "home remodeling",
    "kitchen remodeling",
    "bathroom remodeling",
    "general contractor",
    "painting company",
    "landscaping company",
    "plumbing company",
    "electrical contractor",
    "fence company",
    "pool company",
    "pressure washing",
    "garage door company",
    "window installation",
]

# Simplified niche labels for file naming and DB storage
NICHE_LABELS = {
    "roofing company": "roofing",
    "solar company": "solar",
    "solar installation": "solar",
    "HVAC company": "hvac",
    "HVAC contractor": "hvac",
    "epoxy flooring": "epoxy",
    "epoxy coating": "epoxy",
    "home remodeling": "remodeling",
    "kitchen remodeling": "remodeling",
    "bathroom remodeling": "remodeling",
    "general contractor": "general_contractor",
    "painting company": "painting",
    "landscaping company": "landscaping",
    "plumbing company": "plumbing",
    "electrical contractor": "electrical",
    "fence company": "fencing",
    "pool company": "pool",
    "pressure washing": "pressure_washing",
    "garage door company": "garage_door",
    "window installation": "windows",
}

# --- Locations by state ---
LOCATIONS = {
    "florida": [
        "Miami, FL", "Fort Lauderdale, FL", "West Palm Beach, FL",
        "Orlando, FL", "Tampa, FL", "Jacksonville, FL",
        "St. Petersburg, FL", "Naples, FL", "Sarasota, FL",
        "Cape Coral, FL", "Fort Myers, FL", "Tallahassee, FL",
        "Gainesville, FL", "Daytona Beach, FL", "Ocala, FL",
        "Lakeland, FL", "Pensacola, FL", "Port St. Lucie, FL",
        "Boca Raton, FL", "Clearwater, FL",
    ],
    "texas": [
        "Houston, TX", "Dallas, TX", "San Antonio, TX",
        "Austin, TX", "Fort Worth, TX", "El Paso, TX",
        "Arlington, TX", "Plano, TX", "Frisco, TX", "McKinney, TX",
        "Lubbock, TX", "Corpus Christi, TX", "Laredo, TX",
        "Round Rock, TX", "Sugar Land, TX",
    ],
    "california": [
        "Los Angeles, CA", "San Diego, CA", "San Jose, CA",
        "Sacramento, CA", "Fresno, CA", "Riverside, CA",
        "Bakersfield, CA", "Anaheim, CA", "Santa Ana, CA",
        "Irvine, CA", "Long Beach, CA", "Oakland, CA",
        "San Francisco, CA", "Stockton, CA", "Modesto, CA",
    ],
    "arizona": [
        "Phoenix, AZ", "Scottsdale, AZ", "Tucson, AZ",
        "Mesa, AZ", "Chandler, AZ", "Gilbert, AZ",
        "Tempe, AZ", "Surprise, AZ", "Peoria, AZ", "Goodyear, AZ",
    ],
    "georgia": [
        "Atlanta, GA", "Savannah, GA", "Augusta, GA",
        "Columbus, GA", "Marietta, GA", "Roswell, GA",
        "Sandy Springs, GA", "Alpharetta, GA", "Athens, GA",
        "Macon, GA",
    ],
    "north_carolina": [
        "Charlotte, NC", "Raleigh, NC", "Durham, NC",
        "Greensboro, NC", "Fayetteville, NC", "Wilmington, NC",
        "Asheville, NC", "Winston-Salem, NC", "Cary, NC",
        "High Point, NC",
    ],
    "nevada": [
        "Las Vegas, NV", "Henderson, NV", "Reno, NV",
        "North Las Vegas, NV", "Sparks, NV",
    ],
    "colorado": [
        "Denver, CO", "Colorado Springs, CO", "Aurora, CO",
        "Fort Collins, CO", "Lakewood, CO", "Boulder, CO",
        "Arvada, CO", "Westminster, CO",
    ],
    "tennessee": [
        "Nashville, TN", "Memphis, TN", "Knoxville, TN",
        "Chattanooga, TN", "Clarksville, TN", "Murfreesboro, TN",
        "Franklin, TN", "Johnson City, TN",
    ],
    "ohio": [
        "Columbus, OH", "Cleveland, OH", "Cincinnati, OH",
        "Toledo, OH", "Akron, OH", "Dayton, OH",
        "Canton, OH", "Youngstown, OH",
    ],
    "virginia": [
        "Virginia Beach, VA", "Norfolk, VA", "Richmond, VA",
        "Chesapeake, VA", "Arlington, VA", "Alexandria, VA",
        "Newport News, VA", "Hampton, VA",
    ],
    "south_carolina": [
        "Charleston, SC", "Columbia, SC", "Greenville, SC",
        "Myrtle Beach, SC", "Rock Hill, SC", "Mount Pleasant, SC",
    ],
    "alabama": [
        "Birmingham, AL", "Huntsville, AL", "Montgomery, AL",
        "Mobile, AL", "Tuscaloosa, AL", "Hoover, AL",
    ],
    "louisiana": [
        "New Orleans, LA", "Baton Rouge, LA", "Shreveport, LA",
        "Lafayette, LA", "Lake Charles, LA", "Metairie, LA",
    ],
    "maryland": [
        "Baltimore, MD", "Columbia, MD", "Germantown, MD",
        "Silver Spring, MD", "Annapolis, MD", "Frederick, MD",
    ],
    "indiana": [
        "Indianapolis, IN", "Fort Wayne, IN", "Evansville, IN",
        "South Bend, IN", "Carmel, IN", "Fishers, IN",
    ],
    "missouri": [
        "Kansas City, MO", "St. Louis, MO", "Springfield, MO",
        "Columbia, MO", "Independence, MO", "Lee's Summit, MO",
    ],
    "michigan": [
        "Detroit, MI", "Grand Rapids, MI", "Warren, MI",
        "Ann Arbor, MI", "Lansing, MI", "Sterling Heights, MI",
    ],
    "pennsylvania": [
        "Philadelphia, PA", "Pittsburgh, PA", "Allentown, PA",
        "Erie, PA", "Reading, PA", "Scranton, PA",
    ],
    "new_jersey": [
        "Newark, NJ", "Jersey City, NJ", "Trenton, NJ",
        "Edison, NJ", "Woodbridge, NJ", "Toms River, NJ",
    ],
}

# --- State rotation: 1 state per day, round-robin ---
STATE_ROTATION = list(LOCATIONS.keys())


def get_today_state() -> str:
    """Get which state to scrape today (round-robin by day of year)."""
    day = date.today().timetuple().tm_yday
    idx = day % len(STATE_ROTATION)
    return STATE_ROTATION[idx]


def get_today_locations() -> list[str]:
    """Get the city list for today's state."""
    state = get_today_state()
    return LOCATIONS[state]


# --- Active states (for manual override — empty = use rotation) ---
ACTIVE_STATES = []  # Set e.g. ["florida"] to force a specific state

# --- Scraper settings ---
GOOGLE_MAPS_ACTOR_ID = "compass/crawler-google-places"
MAX_RESULTS_PER_SEARCH = 100
MAX_CRAWLED_PLACES = 100

# --- Email volume target ---
DAILY_EMAIL_TARGET = 1000

# --- Database ---
DB_PATH = "leads.db"

# --- Output ---
CSV_OUTPUT_DIR = "data"
