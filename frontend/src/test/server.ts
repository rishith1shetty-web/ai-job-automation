import { setupServer } from "msw/node";
import { http, HttpResponse } from "msw";

// Base origin matches vitest's jsdom url; the app fetches same-origin "/api/*".
export const BASE = "http://localhost:5173/api";

// Minimal, valid fixtures for the reads the tested pages issue. Individual tests
// override the WRITE handlers (PUT/POST/DELETE) to capture request bodies. These reads
// only exist so a page can render far enough to reach its interactive controls.
export const fixtures = {
  portal: { active_portal: "linkedin", linkedin_connected: true, dice_connected: false },
  resume: {
    id: 6493,
    portal: "linkedin",
    filename: "resume.pdf",
    skills: ["Python"],
    experience: [],
    education: [],
    job_titles: ["Engineer"],
    keywords: [],
    technologies: [],
    domain_expertise: [],
    detected_role: "Software Engineer",
    role_suggestions: [{ role: "Software Engineer", confidence: 90 }],
    generated_search_keywords: [],
    generated_search_query: "software engineer",
    role_confidence: 90,
    created_at: "2026-07-01T00:00:00Z",
  },
  candidateProfile: {
    id: 1,
    portal: "linkedin",
    first_name: "Ada",
    last_name: "Lovelace",
    email: "ada@example.com",
    mobile_phone: "5551234",
    phone_country_code: "+1",
    current_location: "NYC",
    linkedin_url: "",
    portfolio_url: "",
    github_url: "",
    current_job_title: "Engineer",
    current_company: "Acme",
    professional_headline: "Builder",
    professional_links: {},
    field_sources: {},
    missing_required_after_apply: [],
    missing_required_fields: [],
    is_complete: true,
    updated_at: "2026-07-01T00:00:00Z",
  },
  screeningSettings: { mode: "safe", notice_period_default: "30 days", id: 1, updated_at: "2026-07-01T00:00:00Z" },
};

export const handlers = [
  http.get(`${BASE}/portal/active`, () => HttpResponse.json(fixtures.portal)),
  http.get(`${BASE}/resumes/latest`, () => HttpResponse.json(fixtures.resume)),
  http.get(`${BASE}/candidate-profile`, () => HttpResponse.json(fixtures.candidateProfile)),
  http.get(`${BASE}/screening/settings`, () => HttpResponse.json(fixtures.screeningSettings)),
  http.get(`${BASE}/screening/knowledge`, () => HttpResponse.json([])),
  http.get(`${BASE}/employment-history`, () => HttpResponse.json([])),
  http.get(`${BASE}/jobs`, () => HttpResponse.json([])),
  http.get(`${BASE}/applications`, () => HttpResponse.json([])),
  http.get(`${BASE}/dashboard/summary`, () =>
    HttpResponse.json({ matched_jobs: 0, easy_apply_jobs: 0, applied_jobs: 0, needs_review: 0, failed_jobs: 0, skipped_jobs: 0 }),
  ),
  http.get(`${BASE}/applications/apply-top/latest`, () => HttpResponse.json(null)),
  http.get(`${BASE}/applications/apply-top/manual-assist`, () => HttpResponse.json({ active: false })),
  http.get(`${BASE}/applications/apply-top/batches/:id`, () => HttpResponse.json(null)),
];

export const server = setupServer(...handlers);
