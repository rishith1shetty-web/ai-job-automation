// Static fixture data for the Phase 3 UI. These stand in for the real API responses
// so every page renders without any backend call. They were shaped to match the
// Phase 1 contract and captured to resemble realistic Rising Path data. Phase 4 will
// replace imports of these with live TanStack Query hooks — nothing here performs I/O.

import type {
  ApplicationHistory,
  ApplyTopBatch,
  CandidateProfile,
  DashboardSummary,
  EmploymentHistory,
  Job,
  PerformanceSummary,
  PortalStatus,
  ResumeProfile,
  ScreeningAnswer,
  ScreeningSetting,
  SearchBatch,
  SelectedApplicationResult,
} from "@/types";

export const portalStatus: PortalStatus = {
  active_portal: "dice",
  linkedin_connected: false,
  dice_connected: true,
};

export const resume: ResumeProfile = {
  id: 8228,
  portal: "dice",
  filename: "Rishith_Shetty_Resume.pdf",
  skills: [
    "SQL", "Python", "Power BI", "Tableau", "Excel", "ETL", "Data Modeling",
    "Statistics", "A/B Testing", "Snowflake", "dbt", "Airflow",
  ],
  experience: [
    { company: "Acme Analytics", title: "Data Analyst", years: 3 },
    { company: "Northwind Corp", title: "BI Analyst", years: 2 },
  ],
  education: [{ degree: "B.S. Computer Science", school: "University of Pittsburgh" }],
  job_titles: ["Data Analyst", "Business Intelligence Analyst", "Reporting Analyst"],
  keywords: ["data analyst", "sql", "power bi", "dashboards", "reporting"],
  technologies: ["SQL", "Python", "Power BI", "Tableau", "Snowflake", "dbt", "Airflow"],
  domain_expertise: ["Business Intelligence", "Analytics Engineering"],
  detected_role: "Data Analyst",
  role_suggestions: [
    { role: "Data Analyst", confidence: 92, role_category: "primary_role" },
    { role: "Business Intelligence Analyst", confidence: 84, role_category: "strong_fit_roles" },
    { role: "Analytics Engineer", confidence: 71, role_category: "strong_fit_roles" },
    { role: "Reporting Analyst", confidence: 63, role_category: "related_roles" },
    { role: "Data Engineer", confidence: 48, role_category: "stretch_roles" },
  ],
  generated_search_keywords: ["Data Analyst", "Business Intelligence Analyst", "Analytics Engineer"],
  generated_search_query:
    '"Data Analyst" OR "Business Intelligence Analyst" OR "Analytics Engineer"',
  role_confidence: 92,
  created_at: "2026-07-08T15:17:35",
};

export const candidateProfile: CandidateProfile = {
  id: 12,
  portal: "dice",
  first_name: "Rishith",
  last_name: "Shetty",
  email: "rishith@example.com",
  mobile_phone: "5551234567",
  phone_country_code: "+1",
  current_location: "Pittsburgh, PA",
  linkedin_url: "https://linkedin.com/in/rishith",
  portfolio_url: "",
  github_url: "https://github.com/rishith",
  current_job_title: "Data Analyst",
  current_company: "Acme Analytics",
  professional_headline: "Data Analyst | BI & Analytics Engineering",
  professional_links: { github: "https://github.com/rishith", linkedin: "https://linkedin.com/in/rishith" },
  field_sources: {
    first_name: { source: "Resume", confidence: 98 },
    last_name: { source: "Resume", confidence: 98 },
    email: { source: "Resume", confidence: 95 },
    mobile_phone: { source: "Resume", confidence: 90 },
    phone_country_code: { source: "Default", confidence: 70 },
    current_location: { source: "Resume", confidence: 88 },
    linkedin_url: { source: "Resume", confidence: 92 },
    github_url: { source: "Resume", confidence: 92 },
    portfolio_url: { source: "", confidence: 0 },
    current_job_title: { source: "Resume", confidence: 85 },
    current_company: { source: "Resume", confidence: 85 },
  },
  missing_required_after_apply: [],
  missing_required_fields: [],
  is_complete: true,
};

export const summary: DashboardSummary = {
  matched_jobs: 42,
  easy_apply_jobs: 31,
  applied_jobs: 7,
  needs_review: 4,
  failed_jobs: 1,
  skipped_jobs: 2,
};

export const searchBatches: SearchBatch[] = [
  {
    id: "batch-20260708-a",
    resume_id: 8228,
    search_query: '"Data Analyst" OR "Business Intelligence Analyst"',
    location: "United States",
    portal: "dice",
    status: "completed",
    requested_limit: 50,
    current_search_variation: null,
    jobs_collected: 42,
    easy_apply_count: 31,
    duplicates_removed: 8,
    latest_status_message: "Completed. Collected 42 jobs.",
    error_message: null,
    debug_path: "outputs/dice_debug/20260708T151735",
    created_at: "2026-07-08T15:17:35",
    job_count: 42,
  },
  {
    id: "batch-20260707-b",
    resume_id: 8228,
    search_query: '"Analytics Engineer"',
    location: "Remote",
    portal: "dice",
    status: "completed",
    requested_limit: 20,
    current_search_variation: null,
    jobs_collected: 18,
    easy_apply_count: 12,
    duplicates_removed: 3,
    latest_status_message: "Completed. Collected 18 jobs.",
    error_message: null,
    debug_path: "outputs/dice_debug/20260707T101122",
    created_at: "2026-07-07T10:11:22",
    job_count: 18,
  },
];

function makeJob(partial: Partial<Job> & Pick<Job, "id" | "title" | "company" | "match_score">): Job {
  return {
    resume_id: 8228,
    search_batch_id: "batch-20260708-a",
    portal: "dice",
    location: "Pittsburgh, PA",
    job_url: `https://www.dice.com/job/${partial.id}`,
    easy_apply: true,
    form_provider: "Dice Easy Apply",
    search_role: "Data Analyst",
    role_priority: "Primary",
    complexity_score: 40,
    priority_score: 70,
    application_friction_score: 30,
    completion_likelihood_score: 85,
    complexity_level: "Low",
    recommended_apply_priority: "High",
    estimated_steps: 3,
    screening_question_count: 2,
    required_fields: 4,
    unsupported_field_count: 0,
    employment_history_required: false,
    freshness_score: 90,
    semantic_match_score: 82,
    technical_match_score: 88,
    industry_match_score: 76,
    confidence_score: "High",
    matched_skills: ["SQL", "Power BI", "Python"],
    missing_skills: ["Looker"],
    overall_match_summary: "Strong match on core BI and SQL skills.",
    strength_summary: "SQL, dashboarding, and stakeholder reporting align well.",
    weakness_summary: "No Looker experience listed.",
    recommendation_summary: "Apply — high completion likelihood.",
    search_query_used: '"Data Analyst"',
    match_reason: "Matched on SQL, Power BI, reporting.",
    priority_reason: "Primary detected role, high freshness.",
    apply_priority_reason: "Low friction, high completion likelihood.",
    friction_reasons: [],
    status: "matched",
    ...partial,
  };
}

export const jobs: Job[] = [
  makeJob({ id: 33072, title: "Data Analyst", company: "Highmark Health", match_score: 94.2 }),
  makeJob({
    id: 33091, title: "Business Intelligence Analyst", company: "PNC", match_score: 89.7,
    location: "Pittsburgh, PA", search_role: "Business Intelligence Analyst", role_priority: "Secondary",
  }),
  makeJob({
    id: 33110, title: "Analytics Engineer", company: "Duolingo", match_score: 81.3,
    location: "Remote", application_friction_score: 55, complexity_level: "Medium",
    recommended_apply_priority: "Medium", missing_skills: ["dbt Cloud", "Looker"],
  }),
  makeJob({
    id: 33128, title: "Reporting Analyst", company: "UPMC", match_score: 77.9,
    screening_question_count: 4, required_fields: 6, friction_reasons: ["4 screening questions"],
  }),
  makeJob({
    id: 33144, title: "Senior Data Analyst", company: "Aurora Innovation", match_score: 72.5,
    location: "Remote", easy_apply: false, form_provider: "External ATS", status: "unsupported_form",
    application_friction_score: 80, recommended_apply_priority: "Low",
    friction_reasons: ["External ATS", "Employment history required"], employment_history_required: true,
  }),
];

export const applications: ApplicationHistory[] = [
  {
    id: 501, job_id: 33072, portal: "dice", status: "dry_run_ready",
    application_date: "2026-07-08T16:34:00", attempt_count: 1, dry_run: true,
    current_step: "review_page_reached", evidence_path: "outputs/dice_debug/20260708T163400",
    reasoning_type: "screening_match", reasoning_summary: "All required fields satisfied.",
    confidence_score: "High", error_message: null,
    job_title: "Data Analyst", company: "Highmark Health",
    job_url: "https://www.dice.com/job/33072", job_portal: "dice",
  },
  {
    id: 502, job_id: 33128, portal: "dice", status: "needs_review",
    application_date: "2026-07-08T16:36:00", attempt_count: 1, dry_run: true,
    current_step: "missing_required_answer", evidence_path: "outputs/dice_debug/20260708T163600",
    reasoning_type: "screening_gap", reasoning_summary: "One screening question had no confident answer.",
    confidence_score: "Medium", error_message: null,
    job_title: "Reporting Analyst", company: "UPMC",
    job_url: "https://www.dice.com/job/33128", job_portal: "dice",
  },
  {
    id: 503, job_id: 33144, portal: "dice", status: "unsupported_form",
    application_date: "2026-07-08T16:38:00", attempt_count: 1, dry_run: true,
    current_step: "external_ats_detected", evidence_path: null,
    reasoning_type: null, reasoning_summary: null, confidence_score: null,
    error_message: "External ATS application form is not supported.",
    job_title: "Senior Data Analyst", company: "Aurora Innovation",
    job_url: "https://www.dice.com/job/33144", job_portal: "dice",
  },
];

export const applyTopBatch: ApplyTopBatch = {
  id: "apply-20260708-1", resume_id: 8228, portal: "dice", status: "completed",
  dry_run: true, screening_mode: "safe", limit: 10, total_count: 10, current_index: 10,
  current_title: "Reporting Analyst", current_company: "UPMC",
  completed_count: 6, skipped_count: 1, failed_count: 0, needs_review_count: 2, timed_out_count: 1, stopped_count: 0,
  resumable: false, remaining_count: 0, resume_blocked_reason: null,
  latest_status_message: "Completed 10 of 10 jobs (dry run).",
  error_message: null, debug_path: "outputs/apply_top_debug/20260708T163443",
  job_results: [
    { index: 1, title: "Data Analyst", company: "Highmark Health", status: "dry_run_ready" },
    { index: 2, title: "Business Intelligence Analyst", company: "PNC", status: "dry_run_ready" },
    { index: 3, title: "Reporting Analyst", company: "UPMC", status: "needs_review" },
  ],
};

export const selectedApplicationResult: SelectedApplicationResult = {
  job_title: "Data Analyst", company: "Highmark Health",
  status: "dry_run_ready", current_step: "review_page_reached",
  error_message: null, evidence_path: "outputs/dice_debug/20260708T163400",
  evidence_sources: [
    {
      question: "Are you legally authorized to work in the US?", answer: "Yes",
      source: "Candidate profile", confidence: "High",
      reasoning: "Work authorization set to US Citizen.", needs_review: false,
    },
    {
      question: "Current city of residence", answer: "Pittsburgh, PA, USA",
      source: "Resume", confidence: "High",
      reasoning: "Extracted from candidate location and confirmed via autocomplete.", needs_review: false,
    },
    {
      question: "Years of Looker experience", answer: null,
      source: null, confidence: null,
      reasoning: "No confident, evidence-backed answer found.", needs_review: true,
    },
  ],
};

export const screeningSetting: ScreeningSetting = {
  mode: "safe",
  notice_period_default: "30 days",
};

export const screeningAnswers: ScreeningAnswer[] = [
  {
    id: 91, question_text: "What is your expected CTC?", category: "salary",
    answer: "120000", source: "Manual", confidence: "High", currency: "USD",
    evidence: { note: "Recruiter confirmed target salary." },
    normalized_question: "expected ctc", use_count: 3,
  },
  {
    id: 92, question_text: "Years of SQL experience", category: "skill_years",
    answer: "5", source: "Resume", confidence: "Medium", currency: null,
    evidence: { normalized_skill: "SQL" },
    normalized_question: "years of sql experience", use_count: 6,
  },
  {
    id: 93, question_text: "Are you willing to relocate?", category: "yes_no",
    answer: "No", source: "Manual", confidence: "High", currency: null,
    evidence: { note: "Candidate prefers Pittsburgh / remote." },
    normalized_question: "willing to relocate", use_count: 2,
  },
];

export const employmentHistory: EmploymentHistory[] = [
  {
    id: 71, resume_id: 8228, display_order: 0, job_title: "Data Analyst",
    company_name: "Acme Analytics", city: "Pittsburgh", state: "PA", country: "USA",
    employment_type: "Full Time", start_month: "Jan", start_year: 2023,
    end_month: null, end_year: null, currently_work_here: true,
    description: "Built BI dashboards and automated reporting.",
    skills_used: ["SQL", "Power BI"], technologies_used: ["Snowflake", "dbt"],
    confidence: 96, source: "Resume Experience",
  },
  {
    id: 72, resume_id: 8228, display_order: 1, job_title: "BI Analyst",
    company_name: "Northwind Corp", city: "Cleveland", state: "OH", country: "USA",
    employment_type: "Full Time", start_month: "Jun", start_year: 2021,
    end_month: "Dec", end_year: 2022, currently_work_here: false,
    description: "Owned executive reporting suite.",
    skills_used: ["Tableau", "Excel"], technologies_used: ["SQL Server"],
    confidence: 90, source: "Resume Experience",
  },
];

export const reasoning = {
  available: true,
  folder: "outputs/reasoning_debug/20260708T163400",
  question_reasoning: [
    {
      question_text: "Are you authorized to work in the US?", selected_answer: "Yes",
      confidence: "High", reasoning_type: "profile_lookup",
      reasoning_summary: "Work authorization configured as US Citizen.",
      semantic_match_score: 100,
      evidence: { matched_skills: [], missing_skills: [] },
    },
    {
      question_text: "Rate your SQL proficiency", selected_answer: "Expert",
      confidence: "High", reasoning_type: "skill_years",
      reasoning_summary: "5+ years SQL across two roles.",
      semantic_match_score: 92,
      evidence: { matched_skills: ["SQL"], missing_skills: [] },
    },
  ],
  ranking_analysis: [
    {
      job: { title: "Data Analyst", company: "Highmark Health" },
      result: {
        overall_match: 94, semantic_match: 82, technical_match: 88, experience_match: 90,
        education_match: 80, domain_match: 85, industry_match: 76, confidence: "High",
        overall_summary: "Excellent alignment on BI and SQL competencies.",
      },
    },
  ],
  match_explanation: [
    {
      overall_summary: "Candidate is a strong fit for BI and data-analyst roles.",
      strengths: ["Deep SQL", "Dashboarding", "Stakeholder reporting"],
      weaknesses: ["Limited Looker exposure"],
      recommendations: ["Highlight Snowflake + dbt pipeline work"],
    },
  ],
  cover_letter: [
    { cover_letter: "Dear Hiring Manager,\n\nI'm excited to apply for the Data Analyst role..." },
  ],
  resume_suggestions: [
    {
      suggestions: [
        { suggestion: "Quantify dashboard impact", why_it_helps: "Shows measurable business value", source: "AI" },
        { suggestion: "Add a Looker line item if applicable", why_it_helps: "Closes a common skill gap", source: "AI" },
      ],
    },
  ],
  generated_answers: [
    {
      answers: [
        {
          prompt: "Why are you a good fit?", answer: "My BI and SQL background maps directly to this role.",
          confidence: "High", grounding_score: 0.91, evidence_used: ["resume.skills", "employment_history"],
        },
      ],
    },
  ],
  semantic_matches: [
    {
      title: "Data Analyst", company: "Highmark Health",
      semantic_match: { overall_semantic_match: 82, matched_skills: ["SQL", "Power BI"], missing_skills: ["Looker"] },
    },
  ],
  job_description_analysis: { seniority: "Mid", remote: "Hybrid", must_have: ["SQL", "BI tools"] },
};

export const resumeIntelligence = {
  available: true,
  folder: "outputs/resume_intelligence/20260708T163400",
};

export const formLearning = [
  {
    normalized_field: "work_authorization", field_category: "authorization",
    original_labels: ["Are you authorized to work?", "Work authorization"],
    companies_seen: ["Highmark Health", "PNC"], success_rate: 95, confidence: "High",
    times_seen: 12, times_used: 11, last_successful_value_source: "Candidate profile",
    validation_history: [
      { company: "Aurora Innovation", label: "Visa sponsorship", outcome: "needs_review", message: "Ambiguous phrasing" },
    ],
  },
  {
    normalized_field: "current_city", field_category: "location",
    original_labels: ["Current city", "City of residence"],
    companies_seen: ["UPMC", "Duolingo"], success_rate: 88, confidence: "Medium",
    times_seen: 9, times_used: 8, last_successful_value_source: "Resume",
    validation_history: [],
  },
];

export const generalizedForm = [
  {
    portal: "dice", original_field_label: "Are you legally authorized to work in the US?",
    normalized_field_name: "work_authorization", detected_control_type: "radio",
    recommended_fill_strategy: "select_option", times_successful: 11, times_failed: 1,
    last_validation_message: "Selected 'Yes' from candidate profile.",
  },
  {
    portal: "dice", original_field_label: "Current city of residence",
    normalized_field_name: "current_city", detected_control_type: "autocomplete",
    recommended_fill_strategy: "type_and_select_first", times_successful: 8, times_failed: 0,
    last_validation_message: "Committed 'Pittsburgh, PA, USA'.",
  },
];

export const performance: PerformanceSummary = {
  available: true,
  total_runtime_ms: 24700,
  average_apply_time_ms: 8100,
  average_screening_time_ms: 2300,
  average_page_load_ms: 1400,
  average_employment_history_time_ms: 900,
  cache_hits: 34,
  cache_misses: 6,
  slowest_operation: { stage: "easy_apply_application", elapsed_ms: 11200 },
  evidence_path: "outputs/performance/20260708T163443",
};
