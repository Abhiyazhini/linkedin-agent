import os
import streamlit as st
import pandas as pd
from agent import run_recruitment_agent, MAX_CANDIDATES_PER_RUN

st.set_page_config(page_title="AI Talent Sourcing Agent", layout="wide", page_icon="🔍")

st.title("Autonomous LinkedIn Sourcing Agent")
st.caption("Powered by Google Gemini 3.6 Flash + SerpAPI Search Discovery + Scrapingdog Extraction")

with st.sidebar:
    st.header("Architecture & Compliance")
    st.markdown(
        """
- **Model:** Google Gemini 3.6 Flash
- **Discovery:** SerpAPI Google Index (`site:linkedin.com/in/`)
- **Data Enrichment:** Scrapingdog Profile API (Proxycurl Alternative)
- **Validation:** Pydantic Models & `openpyxl` styling
- **Agent Loop:** Gemini decides when to search, enrich, retry, or finish -
  not hardcoded Python control flow
        """
    )
    st.caption(
        "💡 Identical searches/profile fetches are served from a local cache "
        "(`.cache/`) instead of re-calling the APIs, so repeated test runs "
        "during development don't burn free-tier quota."
    )

role_description = st.text_input(
    "Role / skills / location criteria:",
    value="Senior Full Stack Engineers in Bangalore with React and Node.js experience"
)

candidate_count = st.number_input(
    "Number of candidates to source:",
    min_value=1,
    max_value=MAX_CANDIDATES_PER_RUN,
    value=min(2, MAX_CANDIDATES_PER_RUN),
    help=f"Capped at {MAX_CANDIDATES_PER_RUN} per run to protect free-tier API quota. "
         f"Adjust via MAX_CANDIDATES_PER_RUN in .env."
)

query = f"Find {int(candidate_count)} {role_description}"

# Initialize Session State
if "profiles" not in st.session_state:
    st.session_state.profiles = None
if "summary" not in st.session_state:
    st.session_state.summary = None
if "file_path" not in st.session_state:
    st.session_state.file_path = None

if st.button("Run Sourcing Agent", type="primary"):
    status_container = st.status("Agent actively sourcing...", expanded=True)

    def log_callback(msg: str):
        status_container.write(msg)

    profiles, summary, file_path = run_recruitment_agent(query, status_callback=log_callback)
    status_container.update(label="Run Complete", state="complete", expanded=False)

    # Store into session state
    st.session_state.profiles = profiles
    st.session_state.summary = summary
    st.session_state.file_path = file_path

# Render Output persistently
if st.session_state.summary:
    st.subheader("Candidate Summary")
    st.markdown(st.session_state.summary)

if st.session_state.profiles:
    st.subheader("Candidate Roster")
    df_preview = pd.DataFrame([
        {
            "Name": p.full_name,
            "Title": p.current_title,
            "Company": p.current_company,
            "Location": p.location,
            "Skills": ", ".join(p.skills[:5]) if p.skills else "N/A",
            "Profile URL": p.profile_url
        }
        for p in st.session_state.profiles
    ])
    st.dataframe(df_preview, use_container_width=True)
elif st.session_state.summary:
    # Agent ran but found nothing - make that explicit instead of showing a blank section
    st.info("No candidates were found for this query. Try broadening the role, skills, or location.")

if st.session_state.file_path and os.path.exists(st.session_state.file_path):
    with open(st.session_state.file_path, "rb") as f:
        st.download_button(
            label="📥 Download Formatted Excel Report (.xlsx)",
            data=f.read(),
            file_name="Candidates_Report.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            type="primary"
        )