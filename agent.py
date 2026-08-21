import os
import re
import logging
from typing import List, Callable, Tuple, Optional, Dict, Any

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

MODEL_NAME = "gemini-3.6-flash"  # verify this string against current API docs before demo day
MAX_TURNS = 8             # hard safety cap on agent reasoning turns (Gemini calls)
MAX_SEARCH_ATTEMPTS = 3   # hard cap so a stuck agent can't loop searches forever
MAX_CANDIDATES_PER_RUN = int(os.getenv("MAX_CANDIDATES_PER_RUN", "5"))  # quota safety ceiling

SYSTEM_INSTRUCTION = """
You are an autonomous recruitment sourcing agent. You have three tools:

1. search_linkedin_profiles(query, target_count) - discovers candidate profile URLs
   matching a role/skill/location query via public search indexing.
2. fetch_profile_data(profile_url) - enriches a single profile URL into structured
   candidate data (name, title, company, location, skills).
3. finish_sourcing(summary) - call this ONLY when you have enriched at least the
   requested number of qualified candidates (or you have determined, after retrying
   with adjusted search terms, that no matching candidates can be found). The summary
   should be a short, professional note to the recruiter about who was found and why
   they match, or an honest explanation if the search came up empty.

Reasoning rules:
- First identify how many candidates the user wants and what qualifies them.
- Call search_linkedin_profiles to discover candidates.
- Call fetch_profile_data for each promising URL to enrich it.
- If a search returns too few or irrelevant results, you may call
  search_linkedin_profiles again with adjusted/broader terms - but do not repeat the
  exact same query twice.
- Do not call fetch_profile_data twice for the same URL.
- Once you have enough enriched candidates (or have genuinely exhausted reasonable
  search variations), call finish_sourcing with your summary. Do not keep searching
  indefinitely.
"""

FUNCTION_DECLARATIONS = [
    types.FunctionDeclaration(
        name="search_linkedin_profiles",
        description="Discover public LinkedIn profile URLs matching a role/skill/location query.",
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "query": types.Schema(
                    type="STRING",
                    description="Natural-language search query, e.g. role, skills, location."
                ),
                "target_count": types.Schema(
                    type="INTEGER",
                    description="How many candidate profiles to try to find in this search."
                ),
            },
            required=["query", "target_count"],
        ),
    ),
    types.FunctionDeclaration(
        name="fetch_profile_data",
        description="Enrich a single LinkedIn profile URL into structured candidate data.",
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "profile_url": types.Schema(
                    type="STRING",
                    description="The LinkedIn profile URL to enrich."
                ),
            },
            required=["profile_url"],
        ),
    ),
    types.FunctionDeclaration(
        name="finish_sourcing",
        description="Signal that sourcing is complete and provide the final recruiter-facing summary.",
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "summary": types.Schema(
                    type="STRING",
                    description="Professional summary of sourced candidates, or an explanation if none were found."
                ),
            },
            required=["summary"],
        ),
    ),
]

GEMINI_TOOLS = types.Tool(function_declarations=FUNCTION_DECLARATIONS)
GENERATE_CONFIG = types.GenerateContentConfig(
    system_instruction=SYSTEM_INSTRUCTION,
    tools=[GEMINI_TOOLS],
)


def extract_target_count_from_query(query: str) -> int:
    """Best-effort extraction of the requested candidate count.
    Prefers a number immediately following 'find'/'source'/'get' (the common
    phrasing), falls back to the first standalone number, and clamps to a
    sane range so a stray number elsewhere in the query (e.g. '5+ years')
    can't blow up the target count.
    """
    directed = re.search(r'\b(?:find|source|get|need)\s+(\d+)\b', query, re.IGNORECASE)
    if directed:
        count = int(directed.group(1))
    else:
        generic = re.search(r'\b(\d+)\b', query)
        count = int(generic.group(1)) if generic else 2
    return max(1, min(count, MAX_CANDIDATES_PER_RUN))


def _make_function_response_part(name: str, payload: Dict[str, Any]) -> types.Part:
    return types.Part.from_function_response(name=name, response=payload)


def run_recruitment_agent(
    user_query: str,
    status_callback: Optional[Callable[[str], None]] = None,
) -> Tuple[List[ProfileRecord], str, Optional[str]]:

    def status(msg: str):
        logger.info(msg)
        if status_callback:
            status_callback(msg)

    target_count = extract_target_count_from_query(user_query)
    max_fetch_attempts = min(target_count * 2, 12)  # generous buffer for skipped/private profiles, still bounded
    status(f"Targeting {target_count} candidate(s)... (fetch budget: {max_fetch_attempts} calls)")

    collected_profiles: List[ProfileRecord] = []
    fetched_urls: set = set()
    seen_queries: set = set()
    raw_results_by_url: Dict[str, Dict[str, Any]] = {}
    search_attempts = 0
    fetch_attempts = 0
    final_summary: Optional[str] = None

    contents: List[types.Content] = [
        types.Content(role="user", parts=[types.Part(text=user_query)])
    ]

    for turn in range(MAX_TURNS):
        response = client.models.generate_content(
            model=MODEL_NAME, contents=contents, config=GENERATE_CONFIG
        )
        candidate = response.candidates[0]
        contents.append(candidate.content)

        function_calls = [p.function_call for p in candidate.content.parts if p.function_call]

        if not function_calls:
            # Model responded with plain text instead of a tool call - treat as done.
            final_summary = response.text or "Sourcing completed."
            status("Agent finished reasoning without further tool calls.")
            break

        response_parts: List[types.Part] = []
        finished = False

        for fc in function_calls:
            name = fc.name
            args = dict(fc.args) if fc.args else {}

            if name == "search_linkedin_profiles":
                query = args.get("query", user_query)
                count = int(args.get("target_count", target_count))

                if query.strip().lower() in seen_queries:
                    status(f"Agent repeated a search it already tried ('{query}') - skipping to save quota.")
                    payload = {"error": "duplicate_query", "message": "Already searched this exact query. Try different terms or finish sourcing."}
                elif search_attempts >= MAX_SEARCH_ATTEMPTS:
                    status("Search attempt limit reached - asking agent to wrap up.")
                    payload = {"error": "search_limit_reached", "results": []}
                else:
                    search_attempts += 1
                    seen_queries.add(query.strip().lower())
                    status(f"Searching: '{query}' (target {count})")
                    results = search_linkedin_profiles(query=query, target_count=count)
                    for item in results:
                        link = item.get("link", "")
                        if link:
                            raw_results_by_url[link] = item
                    if not results:
                        status("Search returned no results.")
                    payload = {"results": results, "count_found": len(results)}

                response_parts.append(_make_function_response_part(name, payload))

            elif name == "fetch_profile_data":
                url = args.get("profile_url", "")

                if url in fetched_urls:
                    status(f"Agent tried to re-fetch an already-enriched profile - skipping: {url}")
                    payload = {"error": "already_fetched", "message": "This profile was already enriched."}
                elif fetch_attempts >= max_fetch_attempts:
                    status("Fetch attempt limit reached - asking agent to wrap up.")
                    payload = {"error": "fetch_limit_reached"}
                else:
                    fetch_attempts += 1
                    status(f"Enriching profile: {url}")
                    fallback_item = raw_results_by_url.get(url)
                    record = fetch_profile_data(url, fallback_item=fallback_item)
                    collected_profiles.append(record)
                    fetched_urls.add(url)
                    payload = record.model_dump()

                response_parts.append(_make_function_response_part(name, payload))

            elif name == "finish_sourcing":
                final_summary = args.get("summary", "Sourcing completed.")
                status("Agent determined sourcing is complete.")
                response_parts.append(_make_function_response_part(name, {"status": "acknowledged"}))
                finished = True

            else:
                logger.warning(f"Model called unknown tool: {name}")
                response_parts.append(_make_function_response_part(name, {"error": f"unknown tool {name}"}))

        contents.append(types.Content(role="user", parts=response_parts))

        if finished:
            break
    else:
        status("Hit max reasoning turns - wrapping up with what was collected.")

    if final_summary is None:
        if collected_profiles:
            final_summary = (
                f"Sourcing stopped after reaching internal limits. "
                f"{len(collected_profiles)} candidate(s) were enriched before the run ended."
            )
        else:
            final_summary = (
                "No matching candidates could be found for this query. "
                "Consider broadening the role, skills, or location criteria."
            )

    excel_path: Optional[str] = None
    if collected_profiles:
        excel_path = os.path.abspath("output_candidates.xlsx")
        save_to_excel(collected_profiles, excel_path)
        status(f"Excel report saved: {excel_path}")
    else:
        # exporter.save_to_excel() writes nothing and returns a plain string when given
        # an empty list, so there's no file to point to here - leave excel_path as None.
        # app.py already checks `if file_path and os.path.exists(file_path)` before
        # offering a download, so this is safe.
        status("No candidates found - no Excel file to generate.")

    return collected_profiles, final_summary, excel_path