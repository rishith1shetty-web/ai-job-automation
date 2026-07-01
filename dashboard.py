import os

import pandas as pd
import requests
import streamlit as st

API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000")
GET_TIMEOUT_SECONDS = 30
ACTION_TIMEOUT_SECONDS = 240
TABLE_HEIGHT = 520
MAX_TABLE_ROWS = 250

st.set_page_config(page_title="Job Match Console", page_icon="briefcase", layout="wide")
st.title("Job Match Console")


def api_request(method: str, path: str, **kwargs):
    timeout = kwargs.pop("timeout", ACTION_TIMEOUT_SECONDS if method.upper() != "GET" else GET_TIMEOUT_SECONDS)
    response = requests.request(method, f"{API_BASE_URL}{path}", timeout=timeout, **kwargs)
    response.raise_for_status()
    return response.json()


@st.cache_data(ttl=15, show_spinner=False)
def cached_get(path: str):
    return api_request("GET", path, timeout=GET_TIMEOUT_SECONDS)


def clear_cached_reads() -> None:
    cached_get.clear()


def api_error_message(action: str, exc: Exception) -> str:
    if isinstance(exc, requests.Timeout):
        return f"{action} timed out. The backend may still be working; refresh in a moment."
    if isinstance(exc, requests.ConnectionError):
        return f"{action} could not reach FastAPI. Check that the backend is running."
    return f"{action} failed: {exc}"


def show_table(rows, *, column_config=None, height: int = TABLE_HEIGHT) -> None:
    frame = rows if isinstance(rows, pd.DataFrame) else pd.DataFrame(rows)
    if frame.empty:
        st.caption("No rows to display.")
        return
    total = len(frame)
    if total > MAX_TABLE_ROWS:
        st.caption(f"Showing first {MAX_TABLE_ROWS} of {total} rows for faster rendering.")
        frame = frame.head(MAX_TABLE_ROWS)
    st.dataframe(
        frame,
        hide_index=True,
        use_container_width=True,
        height=height,
        column_config=column_config,
    )


try:
    candidate_profile = cached_get("/candidate-profile")
except Exception:
    candidate_profile = None
try:
    screening_settings = cached_get("/screening/settings")
except Exception:
    screening_settings = {"mode": "safe", "notice_period_default": "30 days"}


with st.sidebar:
    st.header("Candidate")
    try:
        linkedin_connected = cached_get("/linkedin/status")["connected"]
    except Exception:
        linkedin_connected = False
    if linkedin_connected:
        st.success("LinkedIn connected")
        if st.button("Disconnect LinkedIn", use_container_width=True):
            with st.spinner("Disconnecting LinkedIn session..."):
                try:
                    api_request("POST", "/linkedin/disconnect")
                    clear_cached_reads()
                    st.success("LinkedIn disconnected")
                    st.rerun()
                except Exception as exc:
                    st.error(api_error_message("LinkedIn disconnect", exc))

    uploaded = st.file_uploader("Resume", type=["pdf", "docx", "txt"])
    if st.button("Analyze resume", type="primary", disabled=uploaded is None, use_container_width=True):
        with st.status("Analyzing resume and extracting profile data...", expanded=True) as status:
            try:
                st.write("Uploading resume...")
                result = api_request(
                    "POST",
                    "/resumes",
                    files={"upload": (uploaded.name, uploaded.getvalue(), uploaded.type)},
                )
                st.write("Refreshing dashboard data...")
                clear_cached_reads()
                st.session_state.resume = result
                st.session_state.pop("jobs", None)
                status.update(label="Resume analyzed", state="complete")
                st.success("Resume analyzed")
            except Exception as exc:
                status.update(label="Resume analysis failed", state="error")
                st.error(api_error_message("Resume analysis", exc))

    if st.button(
        "LinkedIn connected" if linkedin_connected else "Connect LinkedIn",
        disabled=linkedin_connected,
        use_container_width=True,
    ):
        with st.status("Opening LinkedIn login and saving session...", expanded=True) as status:
            try:
                st.write("Waiting for LinkedIn session confirmation...")
                api_request("POST", "/linkedin/connect")
                clear_cached_reads()
                status.update(label="LinkedIn session saved", state="complete")
                st.success("LinkedIn session saved")
                st.rerun()
            except Exception as exc:
                status.update(label="LinkedIn connection failed", state="error")
                st.error(api_error_message("LinkedIn connection", exc))

    st.divider()
    st.subheader("Candidate profile")
    if candidate_profile and candidate_profile.get("is_complete"):
        st.success("Profile complete")
    elif candidate_profile:
        st.info("Profile partially auto-filled")
    else:
        st.info("Profile not loaded")

resume = st.session_state.get("resume")
if resume is None:
    try:
        resume = cached_get("/resumes/latest")
        st.session_state.resume = resume
    except Exception:
        resume = None
elif resume:
    try:
        resume = cached_get(f"/resumes/{resume['id']}")
        st.session_state.resume = resume
    except Exception:
        pass
if resume:
    cols = st.columns(3)
    cols[0].metric("Skills", len(resume["skills"]))
    cols[1].metric("Job titles", len(resume["job_titles"]))
    cols[2].metric("Technologies", len(resume["technologies"]))
    st.caption(f"Active resume: {resume['filename']}")

    st.subheader("Detected candidate profile")
    role_suggestions = resume.get("role_suggestions") or [
        {
            "role": resume.get("detected_role") or "Software Engineer",
            "confidence": resume.get("role_confidence", 0),
        }
    ]
    primary_cols = st.columns([2, 1])
    primary_cols[0].metric("Primary role", role_suggestions[0]["role"])
    primary_cols[1].metric("Confidence", f"{role_suggestions[0]['confidence']:.0f}%")
    role_frame = pd.DataFrame(
        {
            "Rank": range(1, len(role_suggestions) + 1),
            "Suggested role": [item["role"] for item in role_suggestions],
            "Confidence": [item["confidence"] for item in role_suggestions],
        }
    )
    st.dataframe(
        role_frame,
        hide_index=True,
        use_container_width=True,
        column_config={
            "Rank": st.column_config.NumberColumn(width="small"),
            "Confidence": st.column_config.ProgressColumn(
                min_value=0,
                max_value=100,
                format="%.1f%%",
            ),
        },
    )
    st.markdown("**Generated LinkedIn search query**")
    st.code(
        resume.get("generated_search_query")
        or " OR ".join(resume.get("generated_search_keywords") or ["Software Engineer"]),
        language=None,
    )
    if resume["domain_expertise"]:
        st.caption(f"Domain expertise: {', '.join(resume['domain_expertise'])}")

search_batches = []
selected_batch_id = None
if resume:
    try:
        search_batches = cached_get(f"/jobs/search-batches?resume_id={resume['id']}")
    except Exception:
        search_batches = []
if search_batches:
    batch_by_id = {batch["id"]: batch for batch in search_batches}
    desired_batch_id = st.session_state.pop("new_search_batch_id", None)
    batch_ids = list(batch_by_id)
    default_index = batch_ids.index(desired_batch_id) if desired_batch_id in batch_ids else 0
    selected_batch_id = st.selectbox(
        "Search batch",
        options=batch_ids,
        index=default_index,
        key=f"search_batch_{resume['id']}_{len(batch_ids)}",
        format_func=lambda batch_id: (
            f"{batch_by_id[batch_id]['created_at'][:19].replace('T', ' ')} | "
            f"{batch_by_id[batch_id]['location']} | "
            f"{batch_by_id[batch_id]['job_count']} jobs"
        ),
    )
    selected_batch = batch_by_id[selected_batch_id]
    st.caption(f"Search query: {selected_batch['search_query']}")
elif resume:
    st.caption("No saved search batches yet. Existing legacy jobs remain available.")

summary = {
    "matched_jobs": 0,
    "easy_apply_jobs": 0,
    "applied_jobs": 0,
    "needs_review": 0,
    "failed_jobs": 0,
    "skipped_jobs": 0,
}
try:
    summary_path = f"/dashboard/summary?resume_id={resume['id']}" if resume else "/dashboard/summary"
    if selected_batch_id:
        summary_path += f"&search_batch_id={selected_batch_id}"
    summary.update(cached_get(summary_path))
except Exception:
    st.info("Start the FastAPI service to load live data.")

summary_cols = st.columns(4)
summary_cols[0].metric("Matched", summary["matched_jobs"])
summary_cols[1].metric("Easy Apply", summary["easy_apply_jobs"])
summary_cols[2].metric("Applied", summary["applied_jobs"])
summary_cols[3].metric("Needs review", summary["needs_review"])
st.caption(
    f"Automation results: {summary['failed_jobs']} failed · "
    f"{summary['skipped_jobs']} skipped"
)

tab_matches, tab_history, tab_profile, tab_employment, tab_screening, tab_reasoning, tab_form_learning, tab_generalized_form, tab_performance = st.tabs(
    [
        "Matched jobs",
        "Application history",
        "Profile",
        "Employment History",
        "Screening Knowledge",
        "AI Reasoning",
        "AI Form Learning",
        "Generalized Form Engine",
        "Performance",
    ]
)

with tab_matches:
    search_col, location_col, action_col = st.columns([2, 2, 1])
    suggested_roles = [
        item["role"]
        for item in (resume.get("role_suggestions") if resume else []) or []
    ]
    primary_role = suggested_roles[0] if suggested_roles else "Software Engineer"
    search_options = [
        "All suggested roles",
        f"Primary role: {primary_role}",
        *[f"Suggested role: {role}" for role in suggested_roles[1:]],
        "Manual override",
    ]
    location_options = [
        "United States",
        "Remote",
        "New York, NY",
        "Austin, TX",
        "San Francisco, CA",
        "Seattle, WA",
        "Chicago, IL",
        "Boston, MA",
        "Los Angeles, CA",
        "Dallas, TX",
        "Custom",
    ]
    search_choice = search_col.selectbox(
        "Search using",
        search_options,
        help="Search all detected roles, one suggested role, or enter a manual override.",
    )
    location_choice = location_col.selectbox("Location", location_options)
    if search_choice == "All suggested roles":
        keywords = None
    elif search_choice == "Manual override":
        keywords = search_col.text_input("Manual role", placeholder="Enter a job title")
    else:
        keywords = search_choice.split(": ", 1)[1]
    location = (
        location_col.text_input("Custom location", placeholder="City, State or country")
        if location_choice == "Custom"
        else location_choice
    )
    search_disabled = resume is None
    if action_col.button("Find matches", type="primary", disabled=search_disabled, use_container_width=True):
        with st.status("Finding and ranking LinkedIn matches...", expanded=True) as status:
            try:
                st.write("Searching LinkedIn jobs...")
                jobs = api_request(
                    "POST",
                    "/jobs/search",
                    json={
                        "resume_id": resume["id"],
                        "location": location,
                        "keywords": keywords,
                        "limit": 25,
                    },
                )
                st.write("Loading the new search batch...")
                clear_cached_reads()
                refreshed_batches = cached_get(f"/jobs/search-batches?resume_id={resume['id']}")
                if refreshed_batches:
                    st.session_state.new_search_batch_id = refreshed_batches[0]["id"]
                status.update(label=f"Saved a new search batch with {len(jobs)} ranked jobs.", state="complete")
                st.success(f"Saved a new search batch with {len(jobs)} ranked jobs.")
                st.rerun()
            except Exception as exc:
                status.update(label="Job search failed", state="error")
                st.error(api_error_message("Job search", exc))

    try:
        jobs_path = f"/jobs?resume_id={resume['id']}" if resume else "/jobs"
        if selected_batch_id:
            jobs_path += f"&search_batch_id={selected_batch_id}"
        jobs = cached_get(jobs_path)
    except Exception:
        jobs = []

    if jobs:
        frame = pd.DataFrame(jobs)
        desired_columns = [
            "match_score",
            "complexity_score",
            "priority_score",
            "complexity_level",
            "estimated_steps",
            "screening_question_count",
            "required_fields",
            "employment_history_required",
            "freshness_score",
            "semantic_match_score",
            "technical_match_score",
            "industry_match_score",
            "confidence_score",
            "matched_skills",
            "missing_skills",
            "overall_match_summary",
            "strength_summary",
            "weakness_summary",
            "recommendation_summary",
            "easy_apply",
            "title",
            "company",
            "location",
            "search_query_used",
            "match_reason",
            "priority_reason",
            "status",
            "job_url",
        ]
        display = frame[[column for column in desired_columns if column in frame.columns]].copy()
        display = display.rename(
            columns={
                "match_score": "Match",
                "complexity_score": "Complexity Score",
                "priority_score": "Priority Score",
                "complexity_level": "Complexity",
                "estimated_steps": "Estimated Steps",
                "screening_question_count": "Screening Questions",
                "required_fields": "Required Fields",
                "employment_history_required": "Employment History",
                "freshness_score": "Freshness",
                "semantic_match_score": "Semantic Match",
                "technical_match_score": "Technical Match",
                "industry_match_score": "Industry Match",
                "confidence_score": "Confidence",
                "matched_skills": "Matched Skills",
                "missing_skills": "Missing Skills",
                "overall_match_summary": "AI Summary",
                "strength_summary": "Strengths",
                "weakness_summary": "Weaknesses",
                "recommendation_summary": "Recommendations",
                "easy_apply": "Easy Apply",
                "title": "Title",
                "company": "Company",
                "location": "Location",
                "search_query_used": "Search Query",
                "match_reason": "Reason Matched",
                "priority_reason": "Reason Priority",
                "status": "Status",
                "job_url": "Job URL",
            }
        )
        show_table(
            display,
            column_config={
                "Match": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.1f%%"),
                "Complexity Score": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.1f"),
                "Priority Score": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.1f"),
                "Freshness": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.0f"),
                "Semantic Match": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.0f%%"),
                "Technical Match": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.0f%%"),
                "Industry Match": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.0f%%"),
                "Job URL": st.column_config.LinkColumn(display_text="Open"),
            },
        )

        job_by_label = {
            f"{job['title']} | {job['company']} | {job['match_score']:.1f}%"
            + (" | Easy Apply" if job["easy_apply"] else ""): job
            for job in jobs
        }
        selected_label = st.radio(
            "Select one job for individual testing",
            options=list(job_by_label),
            index=None,
        )
        selected_job = job_by_label.get(selected_label) if selected_label else None
        if selected_job:
            st.caption(
                f"Selected job: {selected_job['title']} at {selected_job['company']}"
            )

        selected_col, bulk_col, dry_run_col = st.columns([1, 1, 2])
        dry_run = dry_run_col.toggle("Dry run", value=True)
        screening_mode = (screening_settings.get("mode") or "safe").lower()
        if selected_col.button(
            "Apply Selected Job",
            type="primary",
            disabled=selected_job is None,
            use_container_width=True,
        ):
            with st.status("Processing selected Easy Apply job...", expanded=True) as status:
                try:
                    st.write("Opening job workflow...")
                    result = api_request(
                        "POST",
                        "/applications/apply-selected",
                        json={
                            "resume_id": resume["id"],
                            "job_id": selected_job["id"],
                            "dry_run": dry_run,
                            "screening_mode": screening_mode,
                        },
                    )
                    clear_cached_reads()
                    st.session_state.selected_application_result = {
                        **result,
                        "job_title": selected_job["title"],
                        "company": selected_job["company"],
                    }
                    status.update(label="Selected job processing complete", state="complete")
                except Exception as exc:
                    status.update(label="Selected job processing failed", state="error")
                    st.session_state.selected_application_result = {
                        "job_title": selected_job["title"],
                        "company": selected_job["company"],
                        "status": "failed",
                        "current_step": "request_failed",
                        "error_message": api_error_message("Apply Selected Job", exc),
                        "evidence_path": None,
                    }

        if bulk_col.button(
            "Apply to top 10",
            disabled=resume is None,
            use_container_width=True,
        ):
            with st.status("Processing top Easy Apply jobs...", expanded=True) as status:
                try:
                    st.write("Running the application workflow...")
                    results = api_request(
                        "POST",
                        "/applications/apply-top",
                        json={
                            "resume_id": resume["id"],
                            "limit": 10,
                            "dry_run": dry_run,
                            "search_batch_id": selected_batch_id,
                            "screening_mode": screening_mode,
                        },
                    )
                    clear_cached_reads()
                    status.update(label=f"Processed {len(results)} applications", state="complete")
                    st.success(f"Processed {len(results)} applications")
                    st.rerun()
                except Exception as exc:
                    status.update(label="Application run failed", state="error")
                    st.error(api_error_message("Application run", exc))

        selected_result = st.session_state.get("selected_application_result")
        if selected_result:
            st.subheader("Selected application status")
            result_cols = st.columns(3)
            result_cols[0].metric(
                "Selected job",
                selected_result.get("job_title", "Unknown"),
            )
            result_cols[1].metric(
                "Status",
                str(selected_result.get("status", "unknown")).replace("_", " ").title(),
            )
            result_cols[2].metric(
                "Last step",
                str(selected_result.get("current_step") or "Not started")
                .replace("_", " ")
                .title(),
            )
            if selected_result.get("error_message"):
                if selected_result.get("status") == "failed":
                    st.error(selected_result["error_message"])
                else:
                    st.warning(selected_result["error_message"])
            else:
                st.success("Selected job workflow completed successfully.")
            if selected_result.get("evidence_path"):
                st.caption(f"Evidence saved to: {selected_result['evidence_path']}")
            screening_rows = [
                event
                for event in selected_result.get("run_log", [])
                if event.get("event") == "screening_questions_answered"
            ]
            if screening_rows:
                st.caption("Screening answers were selected during the run. Full JSON logs are under outputs/screening_debug/.")
    else:
        st.caption("No matched jobs yet.")

with tab_history:
    try:
        application_path = f"/applications?resume_id={resume['id']}" if resume else "/applications"
        if selected_batch_id:
            application_path += f"&search_batch_id={selected_batch_id}"
        applications = cached_get(application_path)
    except Exception:
        applications = []
    if applications:
        show_table(applications)
    else:
        st.caption("No application history yet.")

with tab_profile:
    st.subheader("Candidate contact profile")
    with st.form("candidate_profile_form"):
        col_a, col_b = st.columns(2)
        first_name = col_a.text_input(
            "First name",
            value=(candidate_profile or {}).get("first_name") or "",
        )
        last_name = col_b.text_input(
            "Last name",
            value=(candidate_profile or {}).get("last_name") or "",
        )
        email = col_a.text_input(
            "Email address",
            value=(candidate_profile or {}).get("email") or "",
        )
        phone_country_code = col_b.text_input(
            "Phone country code",
            value=(candidate_profile or {}).get("phone_country_code") or "",
            placeholder="+1, +91",
        )
        mobile_phone = col_a.text_input(
            "Mobile phone number",
            value=(candidate_profile or {}).get("mobile_phone") or "",
        )
        current_location = col_b.text_input(
            "Current city/location",
            value=(candidate_profile or {}).get("current_location") or "",
            placeholder="City, State/Country",
        )
        linkedin_url = st.text_input(
            "LinkedIn profile URL",
            value=(candidate_profile or {}).get("linkedin_url") or "",
        )
        current_job_title = col_a.text_input(
            "Current job title",
            value=(candidate_profile or {}).get("current_job_title") or "",
        )
        current_company = col_b.text_input(
            "Current company",
            value=(candidate_profile or {}).get("current_company") or "",
        )
        professional_headline = st.text_input(
            "Professional headline",
            value=(candidate_profile or {}).get("professional_headline") or "",
        )
        portfolio_url = col_a.text_input(
            "Portfolio URL optional",
            value=(candidate_profile or {}).get("portfolio_url") or "",
        )
        github_url = col_b.text_input(
            "GitHub URL optional",
            value=(candidate_profile or {}).get("github_url") or "",
        )
        if st.form_submit_button("Save candidate profile", type="primary"):
            with st.spinner("Saving candidate profile..."):
                try:
                    candidate_profile = api_request(
                        "PUT",
                        "/candidate-profile",
                        json={
                            "first_name": first_name,
                            "last_name": last_name,
                            "email": email,
                            "mobile_phone": mobile_phone,
                            "phone_country_code": phone_country_code,
                            "current_location": current_location,
                            "linkedin_url": linkedin_url,
                            "portfolio_url": portfolio_url,
                            "github_url": github_url,
                            "current_job_title": current_job_title,
                            "current_company": current_company,
                            "professional_headline": professional_headline,
                        },
                    )
                    clear_cached_reads()
                    st.success("Candidate profile saved")
                    st.rerun()
                except Exception as exc:
                    st.error(api_error_message("Candidate profile save", exc))

    if candidate_profile and candidate_profile.get("is_complete"):
        st.success("Candidate profile is complete for standard Easy Apply contact fields.")
    elif candidate_profile:
        st.info(
            "Candidate profile is optional. Missing fields are handled only if a specific Easy Apply form requires them."
        )
        missing_after_apply = candidate_profile.get("missing_required_after_apply") or []
        if missing_after_apply:
            st.warning("Missing required fields from last apply attempt: " + ", ".join(missing_after_apply))

    if candidate_profile:
        st.subheader("Auto-filled field sources")
        source_rows = []
        field_sources = candidate_profile.get("field_sources") or {}
        for label, field in [
            ("First name", "first_name"),
            ("Last name", "last_name"),
            ("Email", "email"),
            ("Mobile phone", "mobile_phone"),
            ("Phone country code", "phone_country_code"),
            ("Location", "current_location"),
            ("LinkedIn URL", "linkedin_url"),
            ("GitHub URL", "github_url"),
            ("Portfolio URL", "portfolio_url"),
            ("Current title", "current_job_title"),
            ("Current company", "current_company"),
        ]:
            metadata = field_sources.get(field, {})
            source_rows.append(
                {
                    "Field": label,
                    "Value": candidate_profile.get(field) or "",
                    "Source": metadata.get("source") or "",
                    "Confidence": metadata.get("confidence") or 0,
                }
            )
        show_table(
            source_rows,
            height=360,
            column_config={
                "Confidence": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.0f%%"),
            },
        )
        links = candidate_profile.get("professional_links") or {}
        if links:
            st.subheader("Professional links")
            st.json(links)

    st.divider()
    if resume:
        st.subheader("Skills")
        st.write(", ".join(resume["skills"]) or "No skills extracted")
        st.subheader("Job titles")
        st.write(", ".join(resume["job_titles"]) or "No job titles extracted")
        st.subheader("Technologies")
        st.write(", ".join(resume["technologies"]) or "No technologies extracted")
        st.subheader("Domain expertise")
        st.write(", ".join(resume["domain_expertise"]) or "No domain expertise inferred")
        st.subheader("Generated search keywords")
        st.write(", ".join(resume["generated_search_keywords"]))
        st.subheader("Generated search query")
        st.code(resume.get("generated_search_query") or "", language=None)
        st.subheader("Education")
        st.json(resume["education"])
        st.subheader("Experience")
        st.json(resume["experience"])
    else:
        st.caption("Upload and analyze a resume to view the profile.")

with tab_employment:
    st.subheader("Employment history")
    try:
        employment_path = f"/employment-history?resume_id={resume['id']}" if resume else "/employment-history"
        employment_records = cached_get(employment_path)
    except Exception:
        employment_records = []
    if employment_records:
        rows = []
        for item in employment_records:
            location = ", ".join(
                part
                for part in [item.get("city"), item.get("state"), item.get("country")]
                if part
            )
            start = " ".join(str(part) for part in [item.get("start_month"), item.get("start_year")] if part)
            end = "Present" if item.get("currently_work_here") else " ".join(
                str(part) for part in [item.get("end_month"), item.get("end_year")] if part
            )
            rows.append(
                {
                    "ID": item["id"],
                    "Company": item.get("company_name") or "",
                    "Job Title": item.get("job_title") or "",
                    "Location": location,
                    "Start Date": start,
                    "End Date": end,
                    "Current Employer": item.get("currently_work_here"),
                    "Description": item.get("description") or "",
                    "Skills": ", ".join(item.get("skills_used") or []),
                    "Technologies": ", ".join(item.get("technologies_used") or []),
                    "Confidence": item.get("confidence") or 0,
                }
            )
        show_table(
            rows,
            column_config={
                "Confidence": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.0f%%"),
            },
        )
    else:
        st.caption("No employment history extracted yet.")

    with st.expander("Add or edit employment"):
        edit_options = {"Add new employment": None}
        edit_options.update(
            {
                f"{item.get('company_name') or 'Unknown'} | {item.get('job_title') or 'Unknown'}": item
                for item in employment_records
            }
        )
        choice = st.selectbox("Record", list(edit_options))
        selected_record = edit_options[choice]
        with st.form("employment_history_form"):
            col_a, col_b = st.columns(2)
            company_name = col_a.text_input("Company", value=(selected_record or {}).get("company_name") or "")
            job_title = col_b.text_input("Job title", value=(selected_record or {}).get("job_title") or "")
            city = col_a.text_input("City", value=(selected_record or {}).get("city") or "")
            state = col_b.text_input("State", value=(selected_record or {}).get("state") or "")
            country = col_a.text_input("Country", value=(selected_record or {}).get("country") or "")
            employment_type = col_b.selectbox(
                "Employment type",
                ["Full Time", "Internship", "Part Time", "Contract", "Freelance"],
                index=["Full Time", "Internship", "Part Time", "Contract", "Freelance"].index(
                    (selected_record or {}).get("employment_type")
                    if (selected_record or {}).get("employment_type") in ["Full Time", "Internship", "Part Time", "Contract", "Freelance"]
                    else "Full Time"
                ),
            )
            start_month = col_a.text_input("Start month", value=(selected_record or {}).get("start_month") or "")
            start_year_value = (selected_record or {}).get("start_year")
            start_year = col_b.number_input("Start year", min_value=1900, max_value=2100, value=int(start_year_value or 2024))
            current = st.checkbox("Currently work here", value=bool((selected_record or {}).get("currently_work_here")))
            end_month = col_a.text_input("End month", value=(selected_record or {}).get("end_month") or "", disabled=current)
            end_year_value = (selected_record or {}).get("end_year")
            end_year = col_b.number_input("End year", min_value=1900, max_value=2100, value=int(end_year_value or 2025), disabled=current)
            description = st.text_area("Description", value=(selected_record or {}).get("description") or "")
            skills = st.text_input("Skills", value=", ".join((selected_record or {}).get("skills_used") or []))
            technologies = st.text_input("Technologies", value=", ".join((selected_record or {}).get("technologies_used") or []))
            submitted = st.form_submit_button("Save employment", type="primary")
            if submitted:
                payload = {
                    "resume_id": resume["id"] if resume else (selected_record or {}).get("resume_id"),
                    "display_order": (selected_record or {}).get("display_order") or len(employment_records),
                    "company_name": company_name,
                    "job_title": job_title,
                    "city": city,
                    "state": state,
                    "country": country,
                    "employment_type": employment_type,
                    "start_month": start_month,
                    "start_year": int(start_year) if start_year else None,
                    "end_month": None if current else end_month,
                    "end_year": None if current else int(end_year),
                    "currently_work_here": current,
                    "description": description,
                    "skills_used": [item.strip() for item in skills.split(",") if item.strip()],
                    "technologies_used": [item.strip() for item in technologies.split(",") if item.strip()],
                    "confidence": (selected_record or {}).get("confidence") or 100,
                    "source": "Manual" if not selected_record else (selected_record.get("source") or "Manual"),
                }
                with st.spinner("Saving employment history..."):
                    try:
                        if selected_record:
                            api_request("PUT", f"/employment-history/{selected_record['id']}", json=payload)
                        else:
                            api_request("POST", "/employment-history", json=payload)
                        clear_cached_reads()
                        st.success("Employment history saved")
                        st.rerun()
                    except Exception as exc:
                        st.error(api_error_message("Employment history save", exc))
        if selected_record and st.button("Delete selected employment"):
            with st.spinner("Deleting employment history..."):
                try:
                    api_request("DELETE", f"/employment-history/{selected_record['id']}")
                    clear_cached_reads()
                    st.success("Employment history deleted")
                    st.rerun()
                except Exception as exc:
                    st.error(api_error_message("Employment history delete", exc))

with tab_screening:
    st.subheader("Screening automation")
    current_mode = (screening_settings.get("mode") or "safe").lower()
    mode_label = "Aggressive Mode" if current_mode == "aggressive" else "Safe Mode"
    with st.form("screening_settings_form"):
        mode_choice = st.selectbox(
            "Screening Automation Mode",
            ["Safe Mode", "Aggressive Mode"],
            index=1 if current_mode == "aggressive" else 0,
        )
        notice_default = st.selectbox(
            "Notice Period Default",
            ["Immediate", "15 days", "30 days", "Custom"],
            index=["Immediate", "15 days", "30 days", "Custom"].index(
                screening_settings.get("notice_period_default")
                if screening_settings.get("notice_period_default") in ["Immediate", "15 days", "30 days", "Custom"]
                else "30 days"
            ),
        )
        custom_notice = ""
        if notice_default == "Custom":
            custom_notice = st.text_input("Custom notice period", value=screening_settings.get("notice_period_default") or "")
        if st.form_submit_button("Save screening settings", type="primary"):
            with st.spinner("Saving screening settings..."):
                try:
                    screening_settings = api_request(
                        "PUT",
                        "/screening/settings",
                        json={
                            "mode": "aggressive" if mode_choice == "Aggressive Mode" else "safe",
                            "notice_period_default": custom_notice if notice_default == "Custom" else notice_default,
                        },
                    )
                    clear_cached_reads()
                    st.success("Screening settings saved")
                    st.rerun()
                except Exception as exc:
                    st.error(api_error_message("Screening settings save", exc))

    if mode_label == "Aggressive Mode":
        st.warning("Aggressive Mode may answer unknown non-sensitive screening questions automatically. Dry Run is recommended for testing.")
    else:
        st.info("Safe Mode leaves unknown screening questions for recruiter review.")

    st.subheader("Saved candidate answers")
    try:
        saved_answers = cached_get("/screening/knowledge")
    except Exception:
        saved_answers = []
    if saved_answers:
        skill_year_rows = []
        for item in saved_answers:
            if item.get("category") == "skill_years":
                evidence = item.get("evidence") or {}
                skill_year_rows.append(
                    {
                        "Skill": evidence.get("normalized_skill") or item.get("question_text", "").replace(" years experience", ""),
                        "Estimated years": item.get("answer"),
                        "Source": item.get("source"),
                        "Confidence": item.get("confidence"),
                    }
                )
        if skill_year_rows:
            st.markdown("**Skill experience estimates**")
            show_table(skill_year_rows, height=260)
        show_table(
            saved_answers,
            column_config={
                "currency": st.column_config.TextColumn("Currency"),
                "confidence": st.column_config.TextColumn("Confidence"),
            },
        )
        delete_options = {
            f"{item['category']} | {item['question_text'][:80]}": item["id"]
            for item in saved_answers
        }
        delete_choice = st.selectbox(
            "Delete saved answer",
            [""] + list(delete_options),
            help="Optional: remove a saved candidate-specific answer.",
        )
        if st.button("Delete answer", disabled=not delete_choice):
            with st.spinner("Deleting saved answer..."):
                try:
                    api_request("DELETE", f"/screening/knowledge/{delete_options[delete_choice]}")
                    clear_cached_reads()
                    st.success("Deleted saved answer")
                    st.rerun()
                except Exception as exc:
                    st.error(api_error_message("Delete saved answer", exc))
    else:
        st.caption("No saved screening answers yet.")

    st.markdown("**Save or update an answer**")
    with st.form("screening_answer_form"):
        question_text = st.text_area(
            "Detected question",
            placeholder="Example: What is your expected CTC?",
        )
        category = st.selectbox(
            "Category",
            ["salary", "skill_years", "yes_no", "skill", "education", "years", "location", "notice", "authorization", "general"],
        )
        answer = st.text_input("Answer selected")
        currency = st.selectbox("Currency", ["", "USD", "INR", "CAD", "GBP", "EUR"])
        confidence = st.selectbox("Confidence", ["High", "Medium", "Estimated / Low"])
        evidence_used = st.text_area(
            "Evidence used",
            placeholder="Example: Recruiter confirmed expected salary for this candidate.",
        )
        if st.form_submit_button("Save answer"):
            with st.spinner("Saving candidate answer..."):
                try:
                    api_request(
                        "POST",
                        "/screening/knowledge",
                        json={
                            "question_text": question_text,
                            "category": category,
                            "answer": answer,
                            "source": "Manual",
                            "confidence": confidence,
                            "currency": currency or None,
                            "evidence": {"note": evidence_used},
                            "resume_id": resume["id"] if resume else None,
                        },
                    )
                    clear_cached_reads()
                    st.success("Saved screening answer for this candidate")
                    st.rerun()
                except Exception as exc:
                    st.error(api_error_message("Save screening answer", exc))

with tab_reasoning:
    st.subheader("AI reasoning")
    try:
        intelligence = cached_get("/resume-intelligence/latest")
    except Exception:
        intelligence = {"available": False}
    if intelligence.get("available"):
        st.markdown("**Resume Intelligence**")
        st.caption(f"Evidence: {intelligence.get('folder')}")
        ranking = intelligence.get("ranking_analysis") or []
        if ranking:
            rows = []
            for item in ranking:
                result = item.get("result") or {}
                job_info = item.get("job") or {}
                rows.append(
                    {
                        "Job": job_info.get("title"),
                        "Company": job_info.get("company"),
                        "Overall Match": result.get("overall_match"),
                        "Semantic Match": result.get("semantic_match"),
                        "Technical Match": result.get("technical_match"),
                        "Experience Match": result.get("experience_match"),
                        "Education Match": result.get("education_match"),
                        "Domain Match": result.get("domain_match"),
                        "Industry Match": result.get("industry_match"),
                        "Confidence": result.get("confidence"),
                        "Summary": result.get("overall_summary"),
                    }
                )
            show_table(
                rows,
                column_config={
                    "Overall Match": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.0f%%"),
                    "Semantic Match": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.0f%%"),
                    "Technical Match": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.0f%%"),
                    "Experience Match": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.0f%%"),
                    "Education Match": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.0f%%"),
                    "Domain Match": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.0f%%"),
                    "Industry Match": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.0f%%"),
                },
            )
        explanations = intelligence.get("match_explanation") or []
        if explanations:
            selected_explanation = explanations[0]
            st.markdown("**Match explanation**")
            st.write(selected_explanation.get("overall_summary"))
            cols = st.columns(3)
            cols[0].markdown("**Strengths**")
            cols[0].write(selected_explanation.get("strengths") or [])
            cols[1].markdown("**Weaknesses**")
            cols[1].write(selected_explanation.get("weaknesses") or [])
            cols[2].markdown("**Recommendations**")
            cols[2].write(selected_explanation.get("recommendations") or [])
        cover_letters = intelligence.get("cover_letter") or []
        if cover_letters:
            st.markdown("**Cover Letter Preview**")
            st.text_area(
                "Generated cover letter",
                value=cover_letters[0].get("cover_letter") or "",
                height=220,
            )
        suggestions = intelligence.get("resume_suggestions") or []
        if suggestions:
            suggestion_rows = []
            for item in suggestions:
                for suggestion in item.get("suggestions") or []:
                    suggestion_rows.append(
                        {
                            "Suggestion": suggestion.get("suggestion"),
                            "Why it helps": suggestion.get("why_it_helps"),
                            "Source": suggestion.get("source"),
                        }
                    )
            if suggestion_rows:
                st.markdown("**Resume Suggestions**")
                show_table(suggestion_rows, height=360)
        generated_answers = intelligence.get("generated_answers") or []
        if generated_answers:
            answer_rows = []
            for item in generated_answers[:1]:
                for answer in item.get("answers") or []:
                    answer_rows.append(
                        {
                            "Prompt": answer.get("prompt"),
                            "Answer": answer.get("answer"),
                            "Confidence": answer.get("confidence"),
                            "Grounding": answer.get("grounding_score"),
                            "Evidence": ", ".join(answer.get("evidence_used") or []),
                        }
                    )
            if answer_rows:
                st.markdown("**Generated AI Answers**")
                show_table(answer_rows, height=360)

    try:
        reasoning = cached_get("/reasoning/latest")
    except Exception:
        reasoning = {"available": False}
    if not reasoning.get("available"):
        st.caption("No reasoning diagnostics have been recorded yet.")
    else:
        st.caption(f"Evidence: {reasoning.get('folder')}")
        question_reasoning = reasoning.get("question_reasoning") or []
        if question_reasoning:
            rows = []
            for item in question_reasoning:
                evidence = item.get("evidence") or {}
                rows.append(
                    {
                        "Question": item.get("question_text"),
                        "Answer": item.get("selected_answer") or item.get("answer"),
                        "Confidence": item.get("confidence"),
                        "Reasoning Type": item.get("reasoning_type"),
                        "Summary": item.get("reasoning_summary") or item.get("reason"),
                        "Semantic Match": item.get("semantic_match_score"),
                        "Matched Skills": ", ".join(evidence.get("matched_skills") or []),
                        "Missing Skills": ", ".join(evidence.get("missing_skills") or []),
                    }
                )
            show_table(rows)
        semantic_matches = reasoning.get("semantic_matches")
        if semantic_matches:
            st.markdown("**Resume vs job description**")
            if isinstance(semantic_matches, list):
                semantic_rows = []
                for item in semantic_matches:
                    match = item.get("semantic_match") or item
                    semantic_rows.append(
                        {
                            "Job": item.get("title"),
                            "Company": item.get("company"),
                            "Semantic Match": match.get("overall_semantic_match"),
                            "Matched Skills": ", ".join(match.get("matched_skills") or []),
                            "Missing Skills": ", ".join(match.get("missing_skills") or []),
                        }
                    )
                show_table(semantic_rows)
            else:
                cols = st.columns(3)
                cols[0].metric("Semantic Match", f"{semantic_matches.get('overall_semantic_match', 0):.0f}%")
                cols[1].metric("Matched Skills", len(semantic_matches.get("matched_skills") or []))
                cols[2].metric("Missing Skills", len(semantic_matches.get("missing_skills") or []))
                st.json(semantic_matches)
        job_description = reasoning.get("job_description_analysis")
        if job_description:
            st.markdown("**Job description metadata**")
            st.json(job_description)

with tab_form_learning:
    st.subheader("AI Form Learning")
    try:
        learned_fields = cached_get("/form-learning")
    except Exception:
        learned_fields = []
    try:
        latest_learning = cached_get("/form-learning/latest")
    except Exception:
        latest_learning = {"available": False}
    if latest_learning.get("available"):
        st.caption(f"Latest evidence: {latest_learning.get('folder')}")
    if learned_fields:
        rows = []
        for item in learned_fields:
            rows.append(
                {
                    "Normalized Field": item.get("normalized_field"),
                    "Category": item.get("field_category"),
                    "Original Labels": ", ".join(item.get("original_labels") or []),
                    "Companies Seen": ", ".join(item.get("companies_seen") or []),
                    "Success Rate": item.get("success_rate"),
                    "Confidence": item.get("confidence"),
                    "Times Learned": item.get("times_seen"),
                    "Times Used": item.get("times_used"),
                    "Last Value Source": item.get("last_successful_value_source"),
                }
            )
        show_table(
            rows,
            column_config={
                "Success Rate": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.0f%%"),
            },
        )
        selected_field = st.selectbox(
            "Validation history",
            options=[item["normalized_field"] for item in learned_fields],
        )
        selected = next((item for item in learned_fields if item["normalized_field"] == selected_field), None)
        if selected:
            history = selected.get("validation_history") or []
            if history:
                show_table(history, height=320)
            else:
                st.caption("No validation failures recorded for this learned field.")
    else:
        st.caption("No learned form fields yet. They will appear after application dry-runs or submissions process fields.")

with tab_generalized_form:
    st.subheader("Generalized Form Engine")
    st.caption("Detects field meaning and fill strategy only. Answers still come from the unified decision pipeline.")
    try:
        generalized_fields = cached_get("/generalized-form-engine")
    except Exception:
        generalized_fields = []
    if generalized_fields:
        show_table(
            generalized_fields,
            column_config={
                "portal": st.column_config.TextColumn("Portal"),
                "original_field_label": st.column_config.TextColumn("Field label"),
                "normalized_field_name": st.column_config.TextColumn("Normalized field"),
                "detected_control_type": st.column_config.TextColumn("Control type"),
                "recommended_fill_strategy": st.column_config.TextColumn("Fill strategy"),
                "times_successful": st.column_config.NumberColumn("Success count"),
                "times_failed": st.column_config.NumberColumn("Failure count"),
                "last_validation_message": st.column_config.TextColumn("Last validation message"),
            },
        )
    else:
        st.caption("No generalized form metadata stored yet.")
    try:
        latest_generalized = cached_get("/generalized-form-engine/latest")
    except Exception:
        latest_generalized = {"available": False}
    if latest_generalized.get("available"):
        st.caption(f"Latest evidence: {latest_generalized.get('folder')}")
        detected = latest_generalized.get("detected_fields") or []
        if detected:
            show_table(detected)
        pipeline = latest_generalized.get("pipeline_integration")
        if pipeline:
            st.markdown("**Pipeline integration**")
            st.json(pipeline)
    else:
        st.caption("No generalized form diagnostics yet.")

with tab_performance:
    try:
        performance = cached_get("/performance/latest")
    except Exception:
        performance = {"available": False}
    if not performance.get("available"):
        st.caption("No performance run has been recorded yet.")
    else:
        perf_cols = st.columns(4)
        perf_cols[0].metric("Total runtime", f"{performance.get('total_runtime_ms', 0) / 1000:.1f}s")
        perf_cols[1].metric("Average Apply", f"{performance.get('average_apply_time_ms', 0) / 1000:.1f}s")
        perf_cols[2].metric("Average Screening", f"{performance.get('average_screening_time_ms', 0) / 1000:.1f}s")
        perf_cols[3].metric("Average Page Load", f"{performance.get('average_page_load_ms', 0) / 1000:.1f}s")
        secondary_cols = st.columns(3)
        secondary_cols[0].metric(
            "Average Employment",
            f"{performance.get('average_employment_history_time_ms', 0) / 1000:.1f}s",
        )
        secondary_cols[1].metric("Cache hits", performance.get("cache_hits", 0))
        secondary_cols[2].metric("Cache misses", performance.get("cache_misses", 0))
        slowest = performance.get("slowest_operation") or {}
        if slowest:
            st.caption(
                f"Slowest operation: {slowest.get('stage')} "
                f"({float(slowest.get('elapsed_ms') or 0) / 1000:.1f}s)"
            )
        if performance.get("evidence_path"):
            st.caption(f"Evidence: {performance['evidence_path']}")
