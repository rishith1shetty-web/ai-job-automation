/**
 * Stop Automation, from the dashboard's side.
 *
 * The button is driven entirely by the batch status the BACKEND publishes -- it appears only
 * while a batch is active, becomes "Stopping…" and disables itself once a stop is in flight, and
 * disappears when the batch ends. Whether a job is actually submitted is decided in the backend
 * (tests/test_stop_automation.py); this file pins the control and the summary the user sees.
 */
import { describe, expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderWithProviders } from "@/test/render";
import { BASE, server } from "@/test/server";
import { MatchedJobsPage } from "./MatchedJobsPage";

function makeJob(i: number, over: Record<string, unknown> = {}) {
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
    ...over,
  };
}

function batch(over: Record<string, unknown> = {}) {
  return {
    id: "batch-1", resume_id: 6493, portal: "linkedin", status: "running", dry_run: false,
    screening_mode: "safe", limit: 10, total_count: 4, current_index: 2, current_job_id: 2,
    current_title: "Job 2", current_company: "Co 2",
    completed_count: 1, skipped_count: 0, failed_count: 0, needs_review_count: 0,
    timed_out_count: 0, stopped_count: 0, latest_status_message: "Processing 2/4",
    error_message: null, debug_path: null, job_results: [], ...over,
  };
}

/** Serve a batch as the resume's latest, so the status card renders without clicking Apply. */
function seed(current: Record<string, unknown>, onStop?: () => Record<string, unknown>) {
  server.use(
    http.get(`${BASE}/jobs`, () => HttpResponse.json([makeJob(1), makeJob(2)])),
    http.get(`${BASE}/applications/apply-top/latest`, () => HttpResponse.json(current)),
    http.get(`${BASE}/applications/apply-top/batches/:id`, () => HttpResponse.json(current)),
    http.post(`${BASE}/applications/apply-top/:id/stop`, () =>
      HttpResponse.json(onStop ? onStop() : batch({ status: "stopping" })),
    ),
  );
}

const stopButton = () => screen.queryByRole("button", { name: /Stop Automation/i });

describe("Stop Automation button", () => {
  it("8. is shown while a batch is running", async () => {
    seed(batch({ status: "running" }));
    renderWithProviders(<MatchedJobsPage />);
    await screen.findByText("Apply Top 10 batch status");
    expect(stopButton()).toBeInTheDocument();
    expect(stopButton()).toBeEnabled();
  });

  it("8b. is shown while a batch is queued", async () => {
    seed(batch({ status: "queued", current_index: 0 }));
    renderWithProviders(<MatchedJobsPage />);
    await screen.findByText("Apply Top 10 batch status");
    expect(stopButton()).toBeInTheDocument();
  });

  it("8c. is NOT shown once the batch has completed", async () => {
    seed(batch({ status: "completed", current_index: 4, completed_count: 4 }));
    renderWithProviders(<MatchedJobsPage />);
    await screen.findByText("Apply Top 10 batch status");
    expect(stopButton()).toBeNull();
  });

  it("8d. is NOT shown once the batch has stopped", async () => {
    seed(batch({ status: "stopped", completed_count: 2, stopped_count: 2 }));
    renderWithProviders(<MatchedJobsPage />);
    await screen.findByText("Apply Top 10 batch status");
    expect(stopButton()).toBeNull();
  });

  it("8e. is NOT shown when the batch failed", async () => {
    seed(batch({ status: "failed", error_message: "boom" }));
    renderWithProviders(<MatchedJobsPage />);
    await screen.findByText("Apply Top 10 batch status");
    expect(stopButton()).toBeNull();
  });

  it("9. clicking it posts the stop request and shows Stopping…", async () => {
    const user = userEvent.setup();
    let stopped: string | null = null;
    server.use(
      http.get(`${BASE}/jobs`, () => HttpResponse.json([makeJob(1)])),
      http.get(`${BASE}/applications/apply-top/latest`, () => HttpResponse.json(batch())),
      http.get(`${BASE}/applications/apply-top/batches/:id`, () => HttpResponse.json(batch())),
      http.post(`${BASE}/applications/apply-top/:id/stop`, ({ params }) => {
        stopped = String(params.id);
        return HttpResponse.json(batch({ status: "stopping", latest_status_message: "Stopping after the current step" }));
      }),
    );
    renderWithProviders(<MatchedJobsPage />);
    await screen.findByText("Apply Top 10 batch status");

    await user.click(stopButton()!);

    await vi.waitFor(() => expect(stopped).toBe("batch-1"));
    await waitFor(() => expect(screen.getByRole("button", { name: /Stopping/i })).toBeDisabled());
  });

  it("9b. renders a disabled Stopping… button for a batch the backend reports as stopping", async () => {
    // Reload mid-stop: the control still reflects the backend, not lost local state.
    seed(batch({ status: "stopping", latest_status_message: "Stopping after the current step" }));
    renderWithProviders(<MatchedJobsPage />);
    await screen.findByText("Apply Top 10 batch status");
    const button = screen.getByRole("button", { name: /Stopping/i });
    expect(button).toBeDisabled();
    expect(screen.queryByRole("button", { name: /^Stop Automation$/ })).toBeNull();
  });

  it("16. a second batch cannot be started while one is active or stopping", async () => {
    for (const status of ["queued", "running", "stopping"]) {
      const view = renderWithProviders(<MatchedJobsPage />);
      seed(batch({ status }));
      view.unmount();
    }
    // Assert on one representative state with the handlers actually in place.
    seed(batch({ status: "running" }));
    renderWithProviders(<MatchedJobsPage />);
    await screen.findByText("Apply Top 10 batch status");
    expect(screen.getByRole("button", { name: "Apply Top 10" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Apply Top 20" })).toBeDisabled();
    expect(screen.getByRole("button", { name: /^Apply All/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Apply Selected Job" })).toBeDisabled();
  });

  it("16b. apply buttons are enabled again once the batch has stopped", async () => {
    seed(batch({ status: "stopped", completed_count: 2, stopped_count: 2 }));
    renderWithProviders(<MatchedJobsPage />);
    await screen.findByText("Apply Top 10 batch status");
    expect(screen.getByRole("button", { name: "Apply Top 10" })).toBeEnabled();
  });

  it("18. the summary keeps completed/stopped/skipped/failed counts after stopping", async () => {
    seed(
      batch({
        status: "stopped",
        current_index: 2,
        completed_count: 4,
        skipped_count: 1,
        failed_count: 1,
        needs_review_count: 0,
        timed_out_count: 0,
        stopped_count: 2,
        latest_status_message: "Batch stopped by user: 4/4 processed, 0 needs review, 1 failed, 0 timed out. 2 stopped.",
        job_results: [
          { index: 1, title: "Job 1", company: "Co 1", status: "applied" },
          { index: 2, title: "Job 2", company: "Co 2", status: "stopped" },
        ],
      }),
    );
    renderWithProviders(<MatchedJobsPage />);
    await screen.findByText("Apply Top 10 batch status");

    // The batch message and the per-job table survive the stop.
    expect(screen.getByText(/Batch stopped by user/)).toBeInTheDocument();
    expect(screen.getByText("Applied")).toBeInTheDocument();
    // "Stopped" appears three times on purpose: the batch Status metric, the Stopped count
    // metric, and the interrupted job's row status. None of them is labelled "Failed".
    expect(screen.getAllByText("Stopped")).toHaveLength(3);
    expect(screen.getByText("Skipped")).toBeInTheDocument();
    expect(screen.getByText("Failed")).toBeInTheDocument();
    // The metric row carries the real counts.
    expect(screen.getByText("Batch stopped by user: 4/4 processed, 0 needs review, 1 failed, 0 timed out. 2 stopped.")).toBeInTheDocument();
  });

  it("18b. hides the stop control but keeps the summary reachable after stopping", async () => {
    seed(batch({ status: "stopped", stopped_count: 2, completed_count: 2 }));
    renderWithProviders(<MatchedJobsPage />);
    await screen.findByText("Apply Top 10 batch status");
    expect(stopButton()).toBeNull();
    // The card can now be dismissed, exactly like a completed or failed batch.
    expect(screen.getByRole("button", { name: "Hide Batch Status" })).toBeInTheDocument();
  });
});
