import os

import time
from urllib.parse import urlencode

import pandas as pd
import requests
import streamlit as st

from app.services.dashboard_tables import build_unique_display_frame

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


def add_query_params(path: str, **params) -> str:
    values = {key: value for key, value in params.items() if value is not None}
    if not values:
        return path
    separator = "&" if "?" in path else "?"
    return f"{path}{separator}{urlencode(values)}"


def clear_cached_reads() -> None:
    cached_get.clear()


def extract_screening_decisions(record: dict) -> list[dict]:
    """Flatten screening decisions from an application record for display.

    Handles both portal log shapes: LinkedIn emits a
    ``screening_questions_answered`` run-log event with an ``answers`` list;
    Dice emits per-field ``field_planned`` entries. Persisted ``evidence_sources``
    is used first when present.
    """
    decisions: list[dict] = []

    for source in record.get("evidence_sources") or []:
        decisions.append(
            {
                "question": source.get("question"),
                "answer": source.get("answer"),
                "source": source.get("source"),
                "confidence": source.get("confidence"),
                "reasoning": source.get("reasoning"),
                "needs_review": source.get("answer") in (None, ""),
            }
        )
    if decisions:
        return decisions

    for event in record.get("run_log") or []:
        if event.get("event") == "screening_questions_answered":
            for answer in event.get("answers", []):
                decisions.append(
                    {
                        "question": answer.get("question_text") or answer.get("question"),
                        "answer": answer.get("selected_answer") or answer.get("answer"),
                        "source": answer.get("source"),
                        "confidence": answer.get("confidence"),
                        "reasoning": answer.get("reason") or answer.get("reasoning_summary"),
                        "needs_review": not (answer.get("selected_answer") or answer.get("answer")),
                    }
                )
        elif event.get("step") == "field_planned" and event.get("category"):
            action = event.get("action")
            if action in {"resume_upload"}:
                continue
            decisions.append(
                {
                    "question": event.get("label"),
                    "answer": event.get("answer"),
                    "source": event.get("source"),
                    "confidence": event.get("confidence"),
                    "reasoning": event.get("reason"),
                    "needs_review": action == "needs_review",
                }
            )
    return decisions


def api_error_message(action: str, exc: Exception) -> str:
    if isinstance(exc, requests.Timeout):
        return f"{action} timed out. The backend may still be working; refresh in a moment."
    if isinstance(exc, requests.ConnectionError):
        return f"{action} could not reach FastAPI. Check that the backend is running."
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        try:
            payload = exc.response.json()
            detail = payload.get("detail")
            if detail:
                return f"{action} failed: {detail}"
        except Exception:
            text = exc.response.text.strip()
            if text:
                return f"{action} failed: {text}"
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


def apply_top_status_card(batch: dict | None) -> None:
    if not batch:
        return
    st.subheader("Apply Top 10 batch status")
    status = str(batch.get("status") or "unknown").replace("_", " ").title()
    st.info(batch.get("latest_status_message") or status)
    metrics = st.columns(5)
    metrics[0].metric("Status", status)
    metrics[1].metric("Current job", f"{batch.get('current_index') or 0}/{batch.get('total_count') or 0}")
    metrics[2].metric("Completed", batch.get("completed_count") or 0)
    metrics[3].metric("Needs review", batch.get("needs_review_count") or 0)
    metrics[4].metric("Failed/Timed out", (batch.get("failed_count") or 0) + (batch.get("timed_out_count") or 0))
    if batch.get("current_title") or batch.get("current_company"):
        st.caption(f"Now processing: {batch.get('current_title') or 'Unknown'} at {batch.get('current_company') or 'Unknown'}")
    if batch.get("debug_path"):
        st.caption(f"Debug log: {batch['debug_path']}")
    if batch.get("error_message"):
        st.error(batch["error_message"])
    if batch.get("job_results"):
        show_table(batch["job_results"], height=260)


def search_batch_status_card(batch: dict | None) -> None:
    if not batch:
        return
    status = str(batch.get("status") or "unknown").replace("_", " ").title()
    st.subheader("Find Matches batch status")
    if batch.get("status") == "failed":
        st.error(batch.get("error_message") or "Search failed in backend.")
    else:
        st.info(batch.get("latest_status_message") or status)
        requested = int(batch.get("requested_limit") or 0)
        found = int(batch.get("jobs_collected") or batch.get("job_count") or 0)
        if batch.get("status") == "completed" and requested and found < requested:
            st.warning(f"Found {found} jobs out of requested {requested} based on current LinkedIn results.")
    metrics = st.columns(5)
    metrics[0].metric("Status", status)
    metrics[1].metric("Jobs collected", batch.get("jobs_collected") or batch.get("job_count") or 0)
    metrics[2].metric("Easy Apply", batch.get("easy_apply_count") or 0)
    metrics[3].metric("Duplicates removed", batch.get("duplicates_removed") or 0)
    metrics[4].metric("Requested", batch.get("requested_limit") or 0)
    if batch.get("current_search_variation"):
        st.caption(f"Current search variation: {batch['current_search_variation']}")
    if batch.get("debug_path"):
        st.caption(f"Debug log: {batch['debug_path']}")


try:
    candidate_profile = cached_get("/candidate-profile")
except Exception:
    candidate_profile = None
try:
    screening_settings = cached_get("/screening/settings")
except Exception:
    screening_settings = {"mode": "safe", "notice_period_default": "30 days"}
try:
    active_status = cached_get("/portal/active")
except Exception:
    active_status = {"active_portal": "linkedin", "linkedin_connected": False, "dice_connected": False}
active_portal = str(active_status.get("active_portal") or "linkedin").lower()
selected_portal = active_portal
portal_choice = active_portal.title()


with st.sidebar:
    st.header("Candidate")
    linkedin_connected = bool(active_status.get("linkedin_connected"))
    dice_connected = bool(active_status.get("dice_connected"))
    st.subheader("Active Portal")
    target_portal = st.radio(
        "Choose Active Portal",
        ["LinkedIn", "Dice"],
        index=1 if active_portal == "dice" else 0,
    ).lower()
    if target_portal != active_portal:
        st.warning(
            f"You are currently using {active_portal.title()}. "
            f"Switching to {target_portal.title()} will disconnect the other portal session."
        )
        if st.button(f"Switch to {target_portal.title()}", type="primary", use_container_width=True):
            with st.spinner(f"Switching to {target_portal.title()}..."):
                try:
                    api_request("POST", f"/portal/active/{target_portal}")
                    clear_cached_reads()
                    st.success(f"Active portal switched to {target_portal.title()}")
                    st.rerun()
                except Exception as exc:
                    st.error(api_error_message("Portal switch", exc))
    st.caption(f"Current active portal: {active_portal.title()}")

    if active_portal == "linkedin":
        if linkedin_connected:
            st.success("LinkedIn connected")
        else:
            st.info("LinkedIn not connected")
        if linkedin_connected and st.button("Disconnect LinkedIn", use_container_width=True):
            with st.spinner("Disconnecting LinkedIn session..."):
                try:
                    api_request("POST", "/linkedin/disconnect")
                    clear_cached_reads()
                    st.success("LinkedIn disconnected")
                    st.rerun()
                except Exception as exc:
                    st.error(api_error_message("LinkedIn disconnect", exc))
    else:
        if dice_connected:
            st.success("Dice connected")
        else:
            st.info("Dice not connected")
        if dice_connected and st.button("Disconnect Dice", use_container_width=True):
            with st.spinner("Disconnecting Dice session..."):
                try:
                    api_request("POST", "/dice/disconnect")
                    clear_cached_reads()
                    st.success("Dice disconnected")
                    st.rerun()
                except Exception as exc:
                    st.error(api_error_message("Dice disconnect", exc))

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

    if active_portal == "linkedin" and st.button(
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

    if active_portal == "dice" and st.button(
        "Dice connected" if dice_connected else "Connect Dice",
        disabled=dice_connected,
        use_container_width=True,
    ):
        with st.status("Opening Dice login. Complete login in the browser window...", expanded=True) as status:
            try:
                st.write("Waiting until Dice login is verified...")
                api_request("POST", "/dice/connect", timeout=660)
                clear_cached_reads()
                status.update(label="Dice session saved", state="complete")
                st.success("Dice session saved")
                st.rerun()
            except Exception as exc:
                status.update(label="Dice connection failed", state="error")
                st.error(api_error_message("Dice connection", exc))

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
            "Category": [str(item.get("role_category") or "").replace("_", " ").title() for item in role_suggestions],
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
    role_groups = {
        "Primary Role": [item["role"] for item in role_suggestions if item.get("role_category") == "primary_role"],
        "Strong Fit Roles": [item["role"] for item in role_suggestions if item.get("role_category") == "strong_fit_roles"],
        "Related Roles": [item["role"] for item in role_suggestions if item.get("role_category") == "related_roles"],
        "Stretch Roles": [item["role"] for item in role_suggestions if item.get("role_category") == "stretch_roles"],
    }
    if any(role_groups.values()):
        group_cols = st.columns(4)
        for col, (label, values) in zip(group_cols, role_groups.items()):
            col.caption(label)
            col.write(", ".join(values) if values else "None")
    st.markdown("**Generated LinkedIn search query**")
    st.code(
        resume.get("generated_search_query")
        or " OR ".join(resume.get("generated_search_keywords") or ["Software Engineer"]),
        language=None,
    )
    if resume["domain_expertise"]:
        st.caption(f"Domain expertise: {', '.join(resume['domain_expertise'])}")

st.info(f"Active Portal: {portal_choice}")

search_batches = []
selected_batch_id = None
if resume:
    try:
        search_batches = cached_get(f"/jobs/search-batches?resume_id={resume['id']}&portal={selected_portal}")
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
            f"{str(batch_by_id[batch_id].get('portal') or 'linkedin').title()} | "
            f"{batch_by_id[batch_id]['location']} | "
            f"{batch_by_id[batch_id]['job_count']} jobs"
        ),
    )
    selected_batch = batch_by_id[selected_batch_id]
    st.caption(f"Search query: {selected_batch['search_query']}")
    if selected_batch.get("status") in {"queued", "running"}:
        try:
            selected_batch = api_request("GET", f"/jobs/search-batches/{selected_batch_id}", timeout=GET_TIMEOUT_SECONDS)
            batch_by_id[selected_batch_id] = selected_batch
        except Exception:
            pass
    if selected_batch.get("status") in {"queued", "running", "failed"}:
        search_batch_status_card(selected_batch)
    if selected_batch.get("status") in {"queued", "running"}:
        clear_cached_reads()
        time.sleep(2)
        st.rerun()
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
    summary_path = add_query_params(summary_path, search_batch_id=selected_batch_id, portal=selected_portal)
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
    custom_search_limit = action_col.number_input(
        "Custom limit",
        min_value=1,
        max_value=100,
        value=100,
        step=5,
        disabled=search_disabled,
    )
    requested_search_limit = None
    if action_col.button("Find 20", disabled=search_disabled, use_container_width=True):
        requested_search_limit = 20
    if action_col.button("Find 50", type="primary", disabled=search_disabled, use_container_width=True):
        requested_search_limit = 50
    if action_col.button("Find 100", disabled=search_disabled, use_container_width=True):
        requested_search_limit = 100
    if action_col.button("Find custom", disabled=search_disabled, use_container_width=True):
        requested_search_limit = min(100, int(custom_search_limit))
    if requested_search_limit is not None:
        with st.status(f"Starting background {portal_choice} search...", expanded=True) as status:
            try:
                st.write(f"Creating a search batch for up to {requested_search_limit} matches...")
                batch = api_request(
                    "POST",
                    "/jobs/search",
                    json={
                        "resume_id": resume["id"],
                        "location": location,
                        "keywords": keywords,
                        "limit": requested_search_limit,
                        "portal": selected_portal,
                    },
                    timeout=GET_TIMEOUT_SECONDS,
                )
                st.write("Search is running in the background.")
                clear_cached_reads()
                st.session_state.new_search_batch_id = batch["id"]
                status.update(label="Search batch started", state="complete")
                st.success("Search batch started. The dashboard will show progress.")
                st.rerun()
            except Exception as exc:
                status.update(label="Could not start search batch", state="error")
                st.error(api_error_message("Start search batch", exc))

    try:
        jobs_path = f"/jobs?resume_id={resume['id']}" if resume else "/jobs"
        jobs_path = add_query_params(jobs_path, search_batch_id=selected_batch_id, portal=selected_portal)
        jobs = cached_get(jobs_path)
    except Exception:
        jobs = []

    if jobs:
        frame = pd.DataFrame(jobs)
        desired_columns = [
            "title",
            "company",
            "portal",
            "form_provider",
            "job_url",
            "search_role",
            "role_priority",
            "location",
            "easy_apply",
            "match_score",
            "complexity_score",
            "priority_score",
            "application_friction_score",
            "completion_likelihood_score",
            "complexity_level",
            "recommended_apply_priority",
            "estimated_steps",
            "screening_question_count",
            "required_fields",
            "unsupported_field_count",
            "employment_history_required",
            "form_provider",
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
            "search_query_used",
            "match_reason",
            "priority_reason",
            "apply_priority_reason",
            "friction_reasons",
            "status",
            "job_url",
        ]
        matched_jobs_rename_map = {
            "match_score": "Match",
            "complexity_score": "Complexity Score",
            "priority_score": "Priority Score",
            "application_friction_score": "Friction",
            "completion_likelihood_score": "Completion Likelihood",
            "complexity_level": "Complexity",
            "recommended_apply_priority": "Apply Priority",
            "estimated_steps": "Estimated Steps",
            "screening_question_count": "Screening Questions",
            "required_fields": "Required Fields",
            "unsupported_field_count": "Unsupported Fields",
            "employment_history_required": "Employment History",
            "form_provider": "Form Provider",
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
            "portal": "Portal",
            "location": "Location",
            "search_query_used": "Search Query",
            "search_role": "Search Role",
            "role_priority": "Role Priority",
            "match_reason": "Reason Matched",
            "priority_reason": "Reason Priority",
            "apply_priority_reason": "Apply Priority Reason",
            "friction_reasons": "Friction Reasons",
            "status": "Status",
            "job_url": "Job URL",
        }
        display = build_unique_display_frame(frame, desired_columns, matched_jobs_rename_map)
        show_table(
            display,
            column_config={
                "Match": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.1f%%"),
                "Complexity Score": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.1f"),
                "Priority Score": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.1f"),
                "Friction": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.1f"),
                "Completion Likelihood": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.1f%%"),
                "Freshness": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.0f"),
                "Semantic Match": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.0f%%"),
                "Technical Match": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.0f%%"),
                "Industry Match": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.0f%%"),
                "Job URL": st.column_config.LinkColumn(display_text="Open"),
            },
        )
        export_columns = [
            "title",
            "company",
            "portal",
            "location",
            "search_role",
            "role_priority",
            "match_score",
            "application_friction_score",
            "recommended_apply_priority",
            "job_url",
        ]
        export_frame = build_unique_display_frame(
            frame,
            export_columns,
            {
                "title": "Title",
                "company": "Company",
                "portal": "Portal",
                "location": "Location",
                "search_role": "Search Role",
                "role_priority": "Role Priority",
                "match_score": "Match",
                "application_friction_score": "Friction",
                "recommended_apply_priority": "Apply Priority",
                "job_url": "Job URL",
            },
        )
        st.download_button(
            "Export matched jobs CSV",
            data=export_frame.to_csv(index=False).encode("utf-8"),
            file_name="matched_jobs.csv",
            mime="text/csv",
            disabled=export_frame.empty,
        )

        job_by_label = {
            f"{str(job.get('portal') or 'linkedin').title()} | {job['title']} | {job['company']} | {job['match_score']:.1f}%"
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

        selected_col, top10_col, top20_col, all_col, dry_run_col = st.columns([1.3, 1, 1, 1, 1.2])
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

        if len(jobs) > 50:
            st.caption("Apply All is safety-capped at 50 ranked jobs per batch.")
        bulk_actions = [
            (top10_col, "Apply Top 10", 10),
            (top20_col, "Apply Top 20", 20),
            (all_col, "Apply All", min(len(jobs), 50)),
        ]
        for column, label, application_limit in bulk_actions:
            if column.button(
                label,
                disabled=resume is None or application_limit <= 0,
                use_container_width=True,
            ):
                with st.status(f"Starting {label} batch...", expanded=True) as status:
                    try:
                        st.write("Starting background workflow...")
                        batch = api_request(
                            "POST",
                            "/applications/apply-top/start",
                            json={
                                "resume_id": resume["id"],
                                "limit": application_limit,
                                "dry_run": dry_run,
                                "search_batch_id": selected_batch_id,
                                "screening_mode": screening_mode,
                                "portal": selected_portal,
                            },
                            timeout=30,
                        )
                        clear_cached_reads()
                        st.session_state.apply_top_batch_id = batch["id"]
                        status.update(label=f"{label} batch started", state="complete")
                        st.success(f"{label} is running in the background. Use Refresh Batch Status to inspect progress.")
                        st.rerun()
                    except Exception as exc:
                        status.update(label=f"Could not start {label} batch", state="error")
                        st.error(api_error_message(f"Start {label}", exc))

        batch_id = st.session_state.get("apply_top_batch_id")
        latest_batch = None
        if batch_id:
            try:
                latest_batch = api_request("GET", f"/applications/apply-top/batches/{batch_id}", timeout=GET_TIMEOUT_SECONDS)
            except Exception as exc:
                st.warning(api_error_message("Refresh batch status", exc))
        elif resume:
            try:
                latest_path = f"/applications/apply-top/latest?resume_id={resume['id']}"
                latest_path += f"&portal={selected_portal}"
                latest_batch = api_request("GET", latest_path, timeout=GET_TIMEOUT_SECONDS)
                if latest_batch:
                    st.session_state.apply_top_batch_id = latest_batch["id"]
            except Exception:
                latest_batch = None
        if latest_batch:
            refresh_col, clear_col = st.columns([1, 4])
            if refresh_col.button("Refresh Batch Status", use_container_width=True):
                clear_cached_reads()
                st.rerun()
            if latest_batch.get("status") in {"completed", "failed"} and clear_col.button("Hide Batch Status"):
                st.session_state.pop("apply_top_batch_id", None)
                st.rerun()
            apply_top_status_card(latest_batch)

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
            screening_decisions = extract_screening_decisions(selected_result)
            if screening_decisions:
                st.markdown("**Screening answers (dry run)**")
                st.dataframe(
                    [
                        {
                            "Question": decision.get("question"),
                            "Answer": decision.get("answer"),
                            "Source": decision.get("source"),
                            "Confidence": decision.get("confidence"),
                            "Reasoning": decision.get("reasoning"),
                        }
                        for decision in screening_decisions
                    ],
                    use_container_width=True,
                    hide_index=True,
                )
                needs_review = [d for d in screening_decisions if d.get("needs_review")]
                if needs_review:
                    st.warning(
                        f"{len(needs_review)} question(s) need review (no confident, evidence-backed answer): "
                        + ", ".join(str(d.get("question")) for d in needs_review[:5])
                    )
                st.caption("Full JSON logs are under outputs/screening_debug/ and outputs/dice_debug/.")
    else:
        st.caption("No matched jobs yet.")

with tab_history:
    try:
        application_path = f"/applications?resume_id={resume['id']}" if resume else "/applications"
        application_path = add_query_params(application_path, search_batch_id=selected_batch_id, portal=selected_portal)
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
