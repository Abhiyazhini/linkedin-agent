import os
import re
import logging
from typing import List, Callable, Tuple
from google import genai
from google.genai import types
from dotenv import load_dotenv

from models import ProfileRecord
from tools import search_linkedin_profiles, fetch_profile_data
from exporter import save_to_excel

load_dotenv()

os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    filename="logs/agent_execution.log",
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("agent_logger")

client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

SYSTEM_INSTRUCTION = """
You are an autonomous recruitment research agent.
Steps:
1. Identify the exact candidate count from user prompt.
2. Run `search_profiles_tool` to discover candidates matching criteria.
3. Provide a clear, professional summary of the sourced candidates.
"""

def extract_target_count_from_query(query: str) -> int:
    match = re.search(r'\b(\d+)\b', query)
    return int(match.group(1)) if match else 2

def run_recruitment_agent(user_query: str, status_callback: Callable[[str], None] = None) -> Tuple[List[ProfileRecord], str, str]:
    target_count = extract_target_count_from_query(user_query)
    if status_callback:
        status_callback(f"Targeting {target_count} candidate(s)...")

    collected_profiles: List[ProfileRecord] = []
    
    # 1. Direct Search & Extraction Pipeline
    raw_results = search_linkedin_profiles(query=user_query, target_count=target_count)
    
    for item in raw_results:
        url = item.get("link", "")
        if status_callback:
            status_callback(f"Enriching profile: {url}")
        record = fetch_profile_data(url, fallback_item=item)
        collected_profiles.append(record)

    # 2. Agent Synthesis with Gemini
    candidate_context = "\n".join([
        f"- {p.full_name} | {p.current_title} at {p.current_company} | Location: {p.location} | URL: {p.profile_url}"
        for p in collected_profiles
    ])

    prompt = f"""
    The following candidates were sourced for the requirement: "{user_query}"
    
    Candidates Data:
    {candidate_context}
    
    Provide a professional summary for the recruiter highlighting why these candidates match the role.
    """

    response = client.models.generate_content(
        model="gemini-3.6-flash",
        contents=prompt
    )
    final_summary = response.text or "Sourcing completed."

    # 3. Always Generate Excel File
    excel_path = os.path.abspath("output_candidates.xlsx")
    save_to_excel(collected_profiles, excel_path)
    
    if status_callback:
        status_callback(f"Excel report saved: {excel_path}")

    return collected_profiles, final_summary, excel_path