# Autonomous LinkedIn Sourcing Agent

An autonomous AI pipeline that translates natural-language hiring requirements into targeted search strategies, discovers candidate profiles across public search indexes, enriches candidate attributes, validates schemas via Pydantic, and exports structured candidate reports to styled Excel spreadsheets.

---

## Architecture Overview
User query: "Find 2 Senior Full Stack Engineers in Bangalore..."
        │
        ▼
┌─────────────────────────────┐
│  Gemini (the agent's brain) │  ← decides what to do next, every turn
└──────────────┬───────────────┘
               │  (tool calls, chosen by the model)
   ┌───────────┼────────────────┬──────────────────┐
   ▼           ▼                ▼                  ▼
search_      fetch_          finish_           (loop continues
linkedin_    profile_        sourcing           until model
profiles     data                               calls finish)
   │           │
   ▼           ▼
SerpAPI    Scrapingdog → (fails?) → snippet fallback parser
(Google       │
 search)      ▼
          Pydantic ProfileRecord (validated)
               │
               ▼
        openpyxl Excel writer
               │
               ▼
        Streamlit UI (live status log, table preview, download button)
 ---

## Tech Stack

| Layer | Technology | Purpose |
| :--- | :--- | :--- |
| **LLM & Reasoning** | Google Gemini 3.6 Flash | Autonomous planning, quota management, and executive candidate synthesis |
| **Discovery Engine** | SerpAPI (Google Search API) | Discovers indexed `site:linkedin.com/in/` URLs without logging into LinkedIn |
| **Profile Enrichment** | Scrapingdog Person Scraper API | Compliant, proxy-managed extraction of public profile fields |
| **Data Modeling** | Pydantic v2 | Enforces type safety, fallback structures, and structured record contracts |
| **Spreadsheet Engine** | Pandas & OpenPyXL | Generates formatted spreadsheets with custom headers and hyperlinked URLs |
| **UI & Interactive Demo** | Streamlit | Real-time status logs, interactive preview table, and `.xlsx` export |
| **Reliability** | Tenacity | Exponential backoff and retry handling on network calls |

---

## Key Engineering Highlights

* **ToS-Compliant Architecture:** Rather than automating authenticated LinkedIn sessions (which violates LinkedIn ToS, triggers account bans, and risks litigation under *hiQ v. LinkedIn*), this project uses public search indexing and compliant API proxies.
* **Resilient Multi-Tier Fallback:** If the profile scraper API encounters rate limits or missing fields, the pipeline automatically parses structured candidate metadata directly from search snippet tokens.
* **Strict Quota Controls:** Prevents token runaways and excessive API credit consumption by parsing exact candidate targets (e.g., "Find 2") directly into tool parameters.
* **Clean State Management:** Streamlit UI leverages `st.session_state` to prevent re-execution and retain candidate tables and download states across interactions.

---

## Project Structure

```text
linkedin-agent/
├── .env                  # API configuration keys
├── .gitignore            # Git exclusion rules
├── requirements.txt      # Python dependencies
├── models.py             # Pydantic data schemas
├── tools.py              # SerpAPI and Scrapingdog retrieval tools
├── exporter.py           # Styled OpenPyXL spreadsheet exporter
├── agent.py              # Gemini orchestration and synthesis loop
├── app.py                # Streamlit user interface
└── README.md             # Architecture and documentation
