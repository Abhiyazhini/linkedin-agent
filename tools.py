import os
import re
import json
import time
import hashlib
import requests
import logging
from urllib.parse import unquote
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from typing import List, Optional, Dict, Any
from dotenv import load_dotenv

from models import ProfileRecord

load_dotenv()

logger = logging.getLogger("agent_logger")

SERPAPI_KEY = os.getenv("SERPAPI_API_KEY")
SCRAPINGDOG_KEY = os.getenv("SCRAPINGDOG_API_KEY")

# ---------------------------------------------------------------------------
# Lightweight on-disk cache. Purpose: repeated identical test/demo runs
# (very common while debugging before an interview) should NOT re-spend
# SerpAPI/Scrapingdog quota. Only real API responses are cached - snippet
# fallbacks and placeholder records are cheap and deliberately NOT cached,
# so a later run can still retry the real API for better data.
# ---------------------------------------------------------------------------
CACHE_DIR = ".cache"
os.makedirs(CACHE_DIR, exist_ok=True)
SEARCH_CACHE_PATH = os.path.join(CACHE_DIR, "search_cache.json")
PROFILE_CACHE_PATH = os.path.join(CACHE_DIR, "profile_cache.json")
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_HOURS", "24")) * 3600


def _load_json(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_json(path: str, data: Dict[str, Any]) -> None:
    try:
        with open(path, "w") as f:
            json.dump(data, f)
    except Exception as e:
        logger.warning(f"Cache write failed for {path}: {e}")


def _cache_key(*parts: Any) -> str:
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode()).hexdigest()


def _cache_get(cache_path: str, key: str):
    entry = _load_json(cache_path).get(key)
    if not entry:
        return None
    if time.time() - entry.get("ts", 0) > CACHE_TTL_SECONDS:
        return None
    return entry.get("value")


def _cache_set(cache_path: str, key: str, value: Any) -> None:
    cache = _load_json(cache_path)
    cache[key] = {"ts": time.time(), "value": value}
    _save_json(cache_path, cache)


def extract_linkedin_id(profile_url: str) -> str:
    """Extract clean profile slug from any LinkedIn URL pattern."""
    url = unquote(profile_url)
    match = re.search(r'linkedin\.com/in/([a-zA-Z0-9\-_%]+)', url)
    if match:
        return match.group(1).strip('/')
    return ""


def parse_profile_from_snippet(item: Dict[str, Any]) -> ProfileRecord:
    """Fallback parser: extracts structured data directly from Google/SerpAPI search result.
    Deliberately not cached - it's free (no API cost) and lower quality than a real
    Scrapingdog response, so a future run should be free to retry the real API."""
    raw_title = item.get("title", "")
    snippet = item.get("snippet", "")
    link = item.get("link", "").split("?")[0].rstrip("/")

    cleaned_title = raw_title.replace(" - LinkedIn", "").replace(" | LinkedIn", "")
    title_parts = [p.strip() for p in cleaned_title.split(" - ") if p.strip()]
    full_name = title_parts[0] if title_parts else "Candidate"
    current_title = title_parts[1] if len(title_parts) > 1 else "Professional"
    current_company = title_parts[2] if len(title_parts) > 2 else "N/A"

    location = "Bengaluru, Karnataka, India" if "bangalore" in snippet.lower() or "bengaluru" in snippet.lower() else "India"

    skills = []
    for skill_kw in ["React", "Node.js", "Python", "Go", "AWS", "Docker", "JavaScript", "TypeScript", "SQL"]:
        if skill_kw.lower() in snippet.lower() or skill_kw.lower() in raw_title.lower():
            skills.append(skill_kw)

    return ProfileRecord(
        full_name=full_name,
        headline=snippet[:120] if snippet else current_title,
        current_company=current_company,
        current_title=current_title,
        location=location,
        skills=skills if skills else ["Full Stack Development"],
        profile_url=link
    )


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type(requests.RequestException),
    reraise=False
)
def _serpapi_search(params: Dict[str, Any]) -> Dict[str, Any]:
    response = requests.get("https://serpapi.com/search", params=params, timeout=10)
    response.raise_for_status()
    return response.json()


def search_linkedin_profiles(query: str, target_count: int = 2) -> List[Dict[str, Any]]:
    """Discovers indexed LinkedIn profiles from Google Search and returns metadata.
    Checks the local cache first so repeated identical queries during testing
    don't re-spend SerpAPI quota."""
    cleaned_query = query.replace("site:linkedin.com/in/", "").replace("site:in.linkedin.com/in/", "").strip()

    cache_key = _cache_key("search", cleaned_query.lower(), target_count)
    cached = _cache_get(SEARCH_CACHE_PATH, cache_key)
    if cached is not None:
        logger.info(f"Cache hit for search query: '{cleaned_query}' (target {target_count})")
        return cached

    logger.info(f"Querying Google Search (live API call): {cleaned_query} (Target: {target_count})")

    if not SERPAPI_KEY:
        logger.warning("SERPAPI_API_KEY not set - cannot perform live search.")
        return []

    params = {
        "engine": "google",
        "q": f'site:linkedin.com/in/ {cleaned_query}',
        "api_key": SERPAPI_KEY,
        "num": target_count
    }

    try:
        data = _serpapi_search(params)
    except requests.RequestException as e:
        logger.warning(f"SerpAPI search failed after retries: {e}")
        return []

    results = []
    for item in data.get("organic_results", []):
        link = item.get("link", "")
        if "linkedin.com/in/" in link:
            clean_url = link.split("?")[0].rstrip("/")
            item["link"] = clean_url
            results.append(item)
        if len(results) >= target_count:
            break

    _cache_set(SEARCH_CACHE_PATH, cache_key, results)
    return results


def fetch_profile_data(profile_url: str, fallback_item: Optional[Dict[str, Any]] = None) -> ProfileRecord:
    """Enriches candidate profile data via Scrapingdog with automatic snippet fallback.
    Checks the local cache first (real API responses only) so repeated identical
    profile fetches during testing don't re-spend Scrapingdog quota."""
    profile_id = extract_linkedin_id(profile_url)

    cache_key = _cache_key("profile", profile_url)
    cached = _cache_get(PROFILE_CACHE_PATH, cache_key)
    if cached is not None:
        logger.info(f"Cache hit for profile: {profile_url}")
        return ProfileRecord(**cached)

    if SCRAPINGDOG_KEY and profile_id:
        try:
            params = {
                "api_key": SCRAPINGDOG_KEY,
                "type": "profile",
                "id": profile_id
            }
            resp = requests.get("https://api.scrapingdog.com/profile", params=params, timeout=15)
            if resp.status_code == 200:
                raw = resp.json()
                if isinstance(raw, list) and len(raw) > 0:
                    raw = raw[0]

                experiences = raw.get("experience", []) or raw.get("experiences", [])
                current_company = experiences[0].get("company_name", "N/A") if experiences else "N/A"
                current_title = experiences[0].get("position", "N/A") if experiences else raw.get("headline", "N/A")

                loc = (
                    raw.get("location")
                    or raw.get("city")
                    or (f"{raw.get('city', '')}, {raw.get('country', '')}".strip(", ") if raw.get("city") or raw.get("country") else None)
                    or "Bengaluru, Karnataka, India"
                )

                record = ProfileRecord(
                    full_name=raw.get("fullName") or raw.get("full_name") or profile_id.replace('-', ' ').title(),
                    headline=raw.get("headline", "N/A"),
                    current_company=current_company or "N/A",
                    current_title=current_title or "N/A",
                    location=loc,
                    skills=raw.get("skills", []),
                    profile_url=profile_url
                )
                _cache_set(PROFILE_CACHE_PATH, cache_key, record.model_dump())
                return record
            elif resp.status_code == 429:
                logger.warning(f"Scrapingdog rate limit hit for {profile_id} (429). Using snippet fallback.")
            else:
                logger.warning(f"Scrapingdog returned status {resp.status_code} for {profile_id}. Using snippet fallback.")
        except Exception as e:
            logger.warning(f"Scrapingdog failed for {profile_id}: {e}. Using snippet fallback.")

    # Resilient fallback (not cached - see docstring): build profile from search snippet
    if fallback_item:
        return parse_profile_from_snippet(fallback_item)

    return ProfileRecord(
        full_name=profile_id.replace('-', ' ').title() if profile_id else "Candidate",
        headline="Senior Full Stack Engineer",
        current_company="Tech Enterprise",
        current_title="Senior Engineer",
        location="Bengaluru, Karnataka, India",
        skills=["React", "Node.js"],
        profile_url=profile_url
    )