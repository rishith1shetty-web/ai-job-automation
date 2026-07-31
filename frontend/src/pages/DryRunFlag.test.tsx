/**
 * Proves what the Dry run toggle actually PUTS ON THE WIRE, for every apply entry point.
 *
 * Bug under investigation: Dice keeps behaving like a dry run with the toggle OFF. The Dice
 * adapter's debug artifacts show dry_run=true arriving, so `false` is lost somewhere between the
 * toggle and the request body. These tests pin the frontend end of that chain.
 *
 * The toggle now starts OFF (normal/live behaviour), so the OFF cases need no click and the ON
 * cases turn it on explicitly. Its name and meaning are unchanged.
 */
import { describe, expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw";
import { screen } from "@testing-library/react";
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

const applyRecord = {
  status: "dry_run_ready", current_step: "done", error_message: null,
  evidence_path: null, evidence_sources: [], run_log: [],
};
const applyBatch = {
  id: "batch-1", resume_id: 6493, portal: "linkedin", status: "queued", dry_run: false,
  screening_mode: "safe", limit: 10, total_count: 0, current_index: 0, current_title: null,
  current_company: null, completed_count: 0, skipped_count: 0, failed_count: 0,
  needs_review_count: 0, timed_out_count: 0, latest_status_message: null, error_message: null,
  debug_path: null, job_results: [],
};

/** Capture the body of whichever apply endpoint fires. */
function captureBodies() {
  const bodies: Record<string, Record<string, unknown>> = {};
  server.use(
    http.post(`${BASE}/applications/apply-selected`, async ({ request }) => {
      bodies["apply-selected"] = (await request.json()) as Record<string, unknown>;
      return HttpResponse.json(applyRecord);
    }),
    http.post(`${BASE}/applications/apply-top/start`, async ({ request }) => {
      bodies["apply-top"] = (await request.json()) as Record<string, unknown>;
      return HttpResponse.json(applyBatch);
    }),
  );
  return bodies;
}

async function render1Job() {
  server.use(http.get(`${BASE}/jobs`, () => HttpResponse.json([makeJob(1)])));
  const view = renderWithProviders(<MatchedJobsPage />);
  await screen.findByText("Job 1");
  return view;
}

const toggle = () => screen.getByRole("switch", { name: "Dry run" });
const jobLabel = (i: number) => `Linkedin | Job ${i} | Co ${i} | 90.0% | Easy Apply`;

describe("dry_run flag on the wire", () => {
  it("1. sends dry_run=false from Apply Selected Job when the toggle is OFF", async () => {
    const user = userEvent.setup();
    const bodies = captureBodies();
    await render1Job();

    expect(toggle()).toHaveAttribute("aria-checked", "false");    // default OFF

    await user.click(screen.getByRole("radio", { name: jobLabel(1) }));
    await user.click(screen.getByRole("button", { name: "Apply Selected Job" }));

    await vi.waitFor(() => expect(bodies["apply-selected"]).toBeDefined());
    expect(bodies["apply-selected"].dry_run).toBe(false);         // not undefined, not true
  });

  it("2. sends dry_run=true from Apply Selected Job once the user turns the toggle ON", async () => {
    const user = userEvent.setup();
    const bodies = captureBodies();
    await render1Job();

    await user.click(toggle());                                   // OFF -> ON
    expect(toggle()).toHaveAttribute("aria-checked", "true");
    await user.click(screen.getByRole("radio", { name: jobLabel(1) }));
    await user.click(screen.getByRole("button", { name: "Apply Selected Job" }));

    await vi.waitFor(() => expect(bodies["apply-selected"]).toBeDefined());
    expect(bodies["apply-selected"].dry_run).toBe(true);
  });

  it("3. sends dry_run=false from every Apply Top entry point when the toggle is OFF", async () => {
    for (const label of [/^Apply Top 10$/, /^Apply Top 20$/, /^Apply All/]) {
      const user = userEvent.setup();
      const bodies = captureBodies();
      const view = await render1Job();

      expect(toggle()).toHaveAttribute("aria-checked", "false");  // default OFF, no click needed
      const button = screen.queryByRole("button", { name: label });
      if (!button) { view.unmount(); continue; }                  // entry point not rendered
      await user.click(button);

      await vi.waitFor(() => expect(bodies["apply-top"]).toBeDefined());
      expect(bodies["apply-top"].dry_run, `${label} sent the wrong dry_run`).toBe(false);
      view.unmount();
    }
  });

  it("4. sends dry_run=true from Apply Top once the user turns the toggle ON", async () => {
    const user = userEvent.setup();
    const bodies = captureBodies();
    await render1Job();

    await user.click(toggle());                                   // OFF -> ON
    expect(toggle()).toHaveAttribute("aria-checked", "true");
    await user.click(screen.getByRole("button", { name: /^Apply Top 10$/ }));

    await vi.waitFor(() => expect(bodies["apply-top"]).toBeDefined());
    expect(bodies["apply-top"].dry_run).toBe(true);
  });
});
