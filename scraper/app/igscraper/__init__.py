"""igscraper - a fast, Apify-compatible Instagram profile scraper."""
from .config import ScraperConfig
from .egress import EgressSpec
from .scraper import ProfileScraper, fetch_profiles, fetch_profiles_sync, run_input, scrape, scrape_profiles

__all__ = ["ScraperConfig", "ProfileScraper", "EgressSpec", "fetch_profiles", "fetch_profiles_sync", "scrape",
           "scrape_profiles", "run_input"]
__version__ = "1.2.0"
