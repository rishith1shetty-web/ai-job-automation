// Types mirroring the FastAPI schema contract (app/schemas.py), captured in the
// Phase 1 migration report. Used by fixtures now (Phase 3) and by the real API
// hooks later (Phase 4). No behavior lives here — pure shape declarations.

export type Portal = "linkedin" | "dice";

export type ApplicationStatus =
  | "matched"
  | "queued"
  | "applied"
  | "needs_review"
  | "skipped"
  | "failed"
  | "dry_run_ready"
  | "unsupported_form";

export type BatchStatus = "queued" | "running" | "completed" | "failed";
export type ScreeningMode = "safe" | "aggressive";

export interface PortalStatus {
  active_portal: Portal;
  linkedin_connected: boolean;
  dice_connected: boolean;
}

export interface RoleSuggestion {
  role: string;
  confidence: number;
  role_category?: string;
}

export interface ResumeProfile {
  id: number;
  portal: Portal;
  filename: string;
  skills: string[];
  experience: Record<string, unknown>[];
  education: Record<string, unknown>[];
  job_titles: string[];
  keywords: string[];
  technologies: string[];
  domain_expertise: string[];
  detected_role: string | null;
  role_suggestions: RoleSuggestion[];
  generated_search_keywords: string[];
  generated_search_query: string | null;
  role_confidence: number;
  created_at: string;
}

export interface Job {
  id: number;
  resume_id: number;
  search_batch_id: string | null;
  portal: Portal;
  title: string;
  company: string;
  location: string | null;
  job_url: string;
  easy_apply: boolean;
  match_score: number;
  form_provider: string | null;
  search_role: string | null;
  role_priority: string | null;
  complexity_score: number;
  priority_score: number;
  application_friction_score: number;
  completion_likelihood_score: number;
  complexity_level: string | null;
  recommended_apply_priority: string | null;
  estimated_steps: number;
  screening_question_count: number;
  required_fields: number;
  unsupported_field_count: number;
  employment_history_required: boolean;
  freshness_score: number;
  semantic_match_score: number;
  technical_match_score: number;
  industry_match_score: number;
  confidence_score: string | null;
  matched_skills: string[];
  missing_skills: string[];
  overall_match_summary: string | null;
  strength_summary: string | null;
  weakness_summary: string | null;
  recommendation_summary: string | null;
  search_query_used: string | null;
  match_reason: string | null;
  priority_reason: string | null;
  apply_priority_reason: string | null;
  friction_reasons: string[];
  status: ApplicationStatus;
}

export interface SearchBatch {
  id: string;
  resume_id: number;
  search_query: string;
  location: string;
  portal: Portal;
  status: BatchStatus;
  requested_limit: number;
  current_search_variation: string | null;
  jobs_collected: number;
  easy_apply_count: number;
  duplicates_removed: number;
  latest_status_message: string | null;
  error_message: string | null;
  debug_path: string | null;
  created_at: string;
  job_count: number;
}

export interface FieldSource {
  source?: string;
  confidence?: number;
}

export interface CandidateProfile {
  id: number;
  portal: Portal;
  first_name: string | null;
  last_name: string | null;
  email: string | null;
  mobile_phone: string | null;
  phone_country_code: string | null;
  current_location: string | null;
  linkedin_url: string | null;
  portfolio_url: string | null;
  github_url: string | null;
  current_job_title: string | null;
  current_company: string | null;
  professional_headline: string | null;
  professional_links: Record<string, string>;
  field_sources: Record<string, FieldSource>;
  missing_required_after_apply: string[];
  missing_required_fields: string[];
  is_complete: boolean;
  created_at?: string;
  updated_at?: string;
}

export interface EmploymentHistory {
  id: number;
  resume_id: number | null;
  display_order: number;
  job_title: string | null;
  company_name: string | null;
  city: string | null;
  state: string | null;
  country: string | null;
  employment_type: string | null;
  start_month: string | null;
  start_year: number | null;
  end_month: string | null;
  end_year: number | null;
  currently_work_here: boolean;
  description: string | null;
  skills_used: string[];
  technologies_used: string[];
  confidence: number;
  source: string;
}

export interface ApplicationHistory {
  id: number;
  job_id: number;
  portal: Portal;
  status: ApplicationStatus;
  application_date: string;
  attempt_count: number;
  dry_run: boolean;
  current_step: string | null;
  evidence_path: string | null;
  reasoning_type: string | null;
  reasoning_summary: string | null;
  confidence_score: string | null;
  error_message: string | null;
  job_title: string;
  company: string;
  job_url: string;
  job_portal: Portal;
}

export interface ApplyTopBatch {
  id: string;
  resume_id: number;
  portal: Portal;
  status: BatchStatus;
  dry_run: boolean;
  screening_mode: string | null;
  limit: number;
  total_count: number;
  current_index: number;
  current_title: string | null;
  current_company: string | null;
  completed_count: number;
  skipped_count: number;
  failed_count: number;
  needs_review_count: number;
  timed_out_count: number;
  latest_status_message: string | null;
  error_message: string | null;
  debug_path: string | null;
  job_results: Record<string, unknown>[];
}

export interface ScreeningSetting {
  mode: ScreeningMode;
  notice_period_default: string | null;
  id?: number;
  updated_at?: string;
}

export interface ScreeningAnswer {
  id: number;
  question_text: string;
  category: string;
  answer: string;
  source: string;
  confidence: string;
  currency: string | null;
  evidence: Record<string, unknown>;
  normalized_question: string;
  use_count: number;
}

export interface DashboardSummary {
  matched_jobs: number;
  easy_apply_jobs: number;
  applied_jobs: number;
  needs_review: number;
  failed_jobs: number;
  skipped_jobs: number;
}

export interface ScreeningDecision {
  question: string | null;
  answer: string | null;
  source: string | null;
  confidence: string | number | null;
  reasoning: string | null;
  needs_review: boolean;
}

export interface SelectedApplicationResult {
  job_title: string;
  company: string;
  status: string;
  current_step: string | null;
  error_message: string | null;
  evidence_path: string | null;
  evidence_sources?: ScreeningDecision[];
}

// Live apply-selected result rendered in the "Selected application status" panel.
// Carries the raw backend record fields (evidence_sources + run_log) so the screening
// decisions can be extracted exactly as dashboard.py does.
export interface ApplyResultView {
  job_title: string;
  company: string;
  status: string;
  current_step: string | null;
  error_message: string | null;
  evidence_path: string | null;
  evidence_sources?: Record<string, unknown>[];
  run_log?: Record<string, unknown>[];
}

export interface PerformanceSummary {
  available: boolean;
  total_runtime_ms: number;
  average_apply_time_ms: number;
  average_screening_time_ms: number;
  average_page_load_ms: number;
  average_employment_history_time_ms: number;
  cache_hits: number;
  cache_misses: number;
  slowest_operation: { stage?: string; elapsed_ms?: number } | null;
  evidence_path: string | null;
}
