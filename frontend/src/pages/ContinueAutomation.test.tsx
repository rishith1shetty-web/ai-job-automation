/**
 * Continue Automation, from the dashboard's side.
 *
 * Which control appears is the BACKEND's decision: `resumable` on the batch already accounts
 * for the batch status, the search session, the resume and the jobs still eligible. The page
 * only has to honour it, and honour it exclusively -- Apply Jobs and Continue Automation must
 * never be offered at the same time. Whether a resumed run actually skips processed jobs is
 * proven in tests/test_continue_automation.py.
 */
import { describe, expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderWithProviders } from "@/test/render";
import { BASE, server } from "@/test/server";
import { MatchedJobsPage } from "./MatchedJobsPage";

function makeJob(i: number) {
  return {
    id: i, resume_id: 6493, search_batch_id: null, portal: "linkedin",
    title: `Job ${i}`, company: `Co ${i}`, location: "Remote", job_url: `https://x/${i}`,
    easy_apply: true, match_score: 90, form_provider: null, search_role: "SWE", role_priority: "High",
    complexity_score: 0, priority_score: 0, application_friction_score: 10, completion_likelihood_score: 0,
    complexity_level: null, recommended_apply_priority: "High", estimated_steps: 0, screening_question_count: 0,
    required_fields: 0, unsupported_field_count: 0, employment_history_required: false, freshness_score: 0,
    semantic_match_score: 0, technical_match_score: 0, industry_match_score: 0, confidence_score: null,
    matched_skills: [], missing_skills: [], overall_match_summary: null, strength_summary: null,
    weakness_summary: null, recommendation_summary: null, search_query_used: null, match_reason: null,
    priority_reason: null, apply_priority_reason: null, friction_reasons: [], status: "matched",
  };
}

function batch(over: Record<string, unknown> = {}) {
  return {
    id: "batch-1", resume_id: 6493, portal: "linkedin", status: "stopped", dry_run: false,
    screening_mode: "safe", limit: 10, total_count: 8, current_index: 3, current_job_id: null,
    current_title: null, current_company: null,
    completed_count: 3, skipped_count: 0, failed_count: 0, needs_review_count: 0,
    timed_out_count: 0, stopped_count: 5,
    resumable: true, remaining_count: 5, resume_blocked_reason: null,
    latest_status_message: "Batch stopped by user: 3/8 processed.",
    error_message: null, debug_path: null, job_results: [], ...over,
  };
}

function seed(current: Record<string, unknown> | null, onResume?: () => Record<string, unknown>) {
  server.use(
    http.get(`${BASE}/jobs`, () => HttpResponse.json([makeJob(1), makeJob(2)])),
    http.get(`${BASE}/applications/apply-top/latest`, () => HttpResponse.json(current)),
    http.get(`${BASE}/applications/apply-top/batches/:id`, () => HttpResponse.json(current)),
    http.post(`${BASE}/applications/apply-top/:id/resume`, () =>
      HttpResponse.json(onResume ? onResume() : batch({ status: "running", resumable: false })),
    ),
  );
}

const continueButton = () => screen.queryByRole("button", { name: /Continue Automation/i });
const applyTop10 = () => screen.queryByRole("button", { name: "Apply Top 10" });

describe("Continue Automation button", () => {
  it("replaces Apply Jobs when the backend reports a resumable batch", async () => {
    seed(batch());
    renderWithProviders(<MatchedJobsPage />);
    await screen.findByText("Apply Top 10 batch status");

    expect(continueButton()).toBeInTheDocument();
    // The three Apply Jobs controls are gone -- never both at once.
    expect(applyTop10()).toBeNull();
    expect(screen.queryByRole("button", { name: "Apply Top 20" })).toBeNull();
    expect(screen.queryByRole("button", { name: /^Apply All/ })).toBeNull();
  });

  it("shows Apply Jobs and no Continue button when there is no batch at all", async () => {
    seed(null);
    renderWithProviders(<MatchedJobsPage />);
    await screen.findByRole("button", { name: "Apply Top 10" });
    expect(continueButton()).toBeNull();
  });

  it("shows Apply Jobs again once the batch has completed", async () => {
    seed(batch({
      status: "completed", completed_count: 8, current_index: 8, stopped_count: 0,
      resumable: false, remaining_count: 0,
      resume_blocked_reason: "This batch is completed, not an interrupted run.",
    }));
    renderWithProviders(<MatchedJobsPage />);
    await screen.findByText("Apply Top 10 batch status");
    expect(continueButton()).toBeNull();
    expect(applyTop10()).toBeInTheDocument();
  });

  it("is not offered while the batch is still running", async () => {
    seed(batch({ status: "running", resumable: false, remaining_count: 0 }));
    renderWithProviders(<MatchedJobsPage />);
    await screen.findByText("Apply Top 10 batch status");
    expect(continueButton()).toBeNull();
    // Running means Stop Automation is the control on offer, unchanged.
    expect(screen.getByRole("button", { name: /Stop Automation/i })).toBeInTheDocument();
  });

  it("is not offered when a new Find Matches has retired the batch", async () => {
    seed(batch({
      resumable: false, remaining_count: 0,
      resume_blocked_reason: "A newer job search has replaced the one this batch was built from.",
    }));
    renderWithProviders(<MatchedJobsPage />);
    await screen.findByText("Apply Top 10 batch status");
    expect(continueButton()).toBeNull();
    expect(applyTop10()).toBeInTheDocument();
  });

  it("appears for a batch the backend adopted as interrupted after a restart", async () => {
    seed(batch({ status: "interrupted", latest_status_message: "Automation was interrupted." }));
    renderWithProviders(<MatchedJobsPage />);
    await screen.findByText("Apply Top 10 batch status");
    expect(continueButton()).toBeInTheDocument();
    expect(applyTop10()).toBeNull();
  });

  it("posts to the existing batch id, never starting a new batch", async () => {
    const user = userEvent.setup();
    let resumedId: string | null = null;
    let startCalls = 0;
    server.use(
      http.get(`${BASE}/jobs`, () => HttpResponse.json([makeJob(1)])),
      http.get(`${BASE}/applications/apply-top/latest`, () => HttpResponse.json(batch())),
      http.get(`${BASE}/applications/apply-top/batches/:id`, () => HttpResponse.json(batch())),
      http.post(`${BASE}/applications/apply-top/start`, () => {
        startCalls += 1;
        return HttpResponse.json(batch({ id: "a-new-batch" }));
      }),
      http.post(`${BASE}/applications/apply-top/:id/resume`, ({ params }) => {
        resumedId = String(params.id);
        return HttpResponse.json(batch({ status: "running", resumable: false }));
      }),
    );
    renderWithProviders(<MatchedJobsPage />);
    await screen.findByText("Apply Top 10 batch status");

    await user.click(continueButton()!);

    await vi.waitFor(() => expect(resumedId).toBe("batch-1"));
    expect(startCalls).toBe(0);
  });

  it("tells the user where the run will pick up and how much is left", async () => {
    seed(batch());
    renderWithProviders(<MatchedJobsPage />);
    await screen.findByText("Apply Top 10 batch status");
    const hint = await screen.findByText(/Continue Automation resumes this batch at job/);
    expect(hint.textContent).toContain("4 of 8");
    expect(hint.textContent).toContain("5 still to process");
  });

  it("surfaces a refusal instead of silently doing nothing", async () => {
    const user = userEvent.setup();
    seed(batch());
    server.use(
      http.post(`${BASE}/applications/apply-top/:id/resume`, () =>
        HttpResponse.json({ detail: "A newer job search has replaced the one this batch was built from." },
          { status: 409 }),
      ),
    );
    renderWithProviders(<MatchedJobsPage />);
    await screen.findByText("Apply Top 10 batch status");

    await user.click(continueButton()!);

    await waitFor(() =>
      expect(screen.getByText(/newer job search has replaced/)).toBeInTheDocument());
  });

  it("appears after the batch ended with an error, alongside the error itself", async () => {
    // The user must be able to recover their progress while the defect is investigated -- the
    // error is still shown, but it is not a dead end.
    seed(batch({
      status: "failed",
      error_message: "TypeError: a defect in the automation",
      latest_status_message:
        "Batch stopped by an error: TypeError: a defect in the automation. " +
        "Continue Automation resumes it from the next unprocessed job.",
    }));
    renderWithProviders(<MatchedJobsPage />);
    await screen.findByText("Apply Top 10 batch status");

    expect(continueButton()).toBeInTheDocument();
    expect(applyTop10()).toBeNull();
    // Twice on purpose: the batch status line and the error alert. The failure is reported in
    // full; it just is not a dead end any more.
    expect(screen.getAllByText(/TypeError: a defect in the automation/)).toHaveLength(2);
    expect(screen.getByText(/Continue Automation resumes it from the next unprocessed job/))
      .toBeInTheDocument();
  });

  it("does not appear for a failed batch with nothing left to process", async () => {
    seed(batch({
      status: "failed", error_message: "Resume not found.",
      resumable: false, remaining_count: 0,
      resume_blocked_reason: "No eligible jobs remain in this batch.",
    }));
    renderWithProviders(<MatchedJobsPage />);
    await screen.findByText("Apply Top 10 batch status");
    expect(continueButton()).toBeNull();
    expect(applyTop10()).toBeInTheDocument();
  });

  it("keeps Stop Automation available once the resumed run is going", async () => {
    seed(batch({ status: "running", resumable: false, current_index: 4, current_title: "Job 4" }));
    renderWithProviders(<MatchedJobsPage />);
    await screen.findByText("Apply Top 10 batch status");
    expect(screen.getByRole("button", { name: /Stop Automation/i })).toBeEnabled();
    expect(continueButton()).toBeNull();
  });
});
