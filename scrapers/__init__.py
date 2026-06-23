"""
Scraper-registry.
Tilføj en ny platform:
  1. Opret scrapers/minside.py med SOURCE_ID, SOURCE_NAME og scrape_all(config) -> (listings, waitlists)
  2. Importér og tilføj til SCRAPERS nedenfor
"""
from scrapers import waitly

SCRAPERS = [
    waitly,
    # boligsiden,
]


def run_all(config: dict) -> tuple[list[dict], list[dict]]:
    """
    Kør alle scrapers. Returnerer (listings, waitlists).
    Hver scraper logger selv ind og returnerer begge lister i én session.
    """
    all_listings  = []
    all_waitlists = []

    for scraper in SCRAPERS:
        try:
            listings, waitlists = scraper.scrape_all(config)
            all_listings.extend(listings)
            all_waitlists.extend(waitlists)
            print(f'[scrapers] {scraper.SOURCE_NAME}: {len(listings)} boliger, {len(waitlists)} ventelister')
        except Exception as e:
            print(f'[scrapers] {scraper.SOURCE_NAME} fejl: {e}')

    return all_listings, all_waitlists
