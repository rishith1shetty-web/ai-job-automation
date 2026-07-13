import { afterEach, describe, expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderWithProviders } from "@/test/render";
import { BASE, fixtures, server } from "@/test/server";
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
const jobLabel = (i: number) => `Linkedin | Job ${i} | Co ${i} | 90.0% | Easy Apply`;

const applyRecord = {
  status: "dry_run_ready", current_step: "done", error_message: null,
  evidence_path: null, evidence_sources: [], run_log: [],
};
const applyBatch = {
  id: "batch-1", resume_id: 6493, portal: "linkedin", status: "queued", dry_run: true,
  screening_mode: "safe", limit: 10, total_count: 0, current_index: 0, current_title: null,
  current_company: null, completed_count: 0, skipped_count: 0, failed_count: 0,
  needs_review_count: 0, timed_out_count: 0, latest_status_message: null, error_message: null,
  debug_path: null, job_results: [],
};

async function renderWithJobs(jobs: unknown[]) {
  server.use(http.get(`${BASE}/jobs`, () => HttpResponse.json(jobs)));
  const view = renderWithProviders(<MatchedJobsPage />);
  await screen.findByText("Job 1"); // table rendered => resume + portal + jobs loaded
  return view;
}

describe("MatchedJobsPage apply request fidelity", () => {
  it("defaults the Dry run toggle to ON", async () => {
    await renderWithJobs([makeJob(1)]);
    expect(screen.getByRole("switch", { name: "Dry run" })).toHaveAttribute("aria-checked", "true");
  });

  it("Apply Selected Job POSTs resume_id/job_id/dry_run/screening_mode and NO portal", async () => {
    let body: Record<string, unknown> | null = null;
    server.use(
      http.post(`${BASE}/applications/apply-selected`, async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(applyRecord);
      }),
    );
    await renderWithJobs([makeJob(1)]);

    await userEvent.click(screen.getByRole("radio", { name: jobLabel(1) }));
    await userEvent.click(screen.getByRole("button", { name: "Apply Selected Job" }));

    await waitFor(() => expect(body).not.toBeNull());
    expect(body!).not.toHaveProperty("portal");
    expect(body!).toEqual({ resume_id: 6493, job_id: 1, dry_run: true, screening_mode: "safe" });
  });

  it("Apply Top 10 POSTs the portal + limit 10 to /apply-top/start", async () => {
    let body: Record<string, unknown> | null = null;
    server.use(
      http.post(`${BASE}/applications/apply-top/start`, async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(applyBatch);
      }),
    );
    await renderWithJobs([makeJob(1), makeJob(2)]);

    await userEvent.click(screen.getByRole("button", { name: "Apply Top 10" }));

    await waitFor(() => expect(body).not.toBeNull());
    expect(body!.portal).toBe("linkedin");
    expect(body!.limit).toBe(10);
    expect(body!.dry_run).toBe(true);
    expect(body!.screening_mode).toBe("safe");
    expect(body!.search_batch_id).toBeNull();
  });

  it("Apply All is safety-capped at 50 (min(job_count, 50))", async () => {
    let body: Record<string, unknown> | null = null;
    server.use(
      http.post(`${BASE}/applications/apply-top/start`, async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(applyBatch);
      }),
    );
    await renderWithJobs(Array.from({ length: 51 }, (_, i) => makeJob(i + 1)));
    expect(screen.getByText("Apply All is safety-capped at 50 ranked jobs per batch.")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Apply All" }));
    await waitFor(() => expect(body).not.toBeNull());
    expect(body!.limit).toBe(50);
  });
});

describe("MatchedJobsPage search keyword parity", () => {
  it("sends the raw manual-override value (empty string, matching Streamlit's text_input)", async () => {
    let body: Record<string, unknown> | null = null;
    server.use(
      http.post(`${BASE}/jobs/search`, async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({ id: "b1", status: "queued" });
      }),
    );
    const { queryClient } = renderWithProviders(<MatchedJobsPage />);
    await waitFor(() => {
      expect(queryClient.getQueryData(["resume", "latest"])).toBeTruthy();
      expect(queryClient.getQueryData(["portal", "active"])).toBeTruthy();
    });

    await userEvent.selectOptions(screen.getAllByRole("combobox")[0], "Manual override");
    await userEvent.click(screen.getByRole("button", { name: "Find 50" }));

    await waitFor(() => expect(body).not.toBeNull());
    expect(body!.keywords).toBe(""); // NOT null
    expect(body!.limit).toBe(50);
    expect(body!.portal).toBe("linkedin");
  });

  it("preserves ': ' inside a suggested role name (Python split(': ', 1))", async () => {
    server.use(
      http.get(`${BASE}/resumes/latest`, () =>
        HttpResponse.json({
          ...fixtures.resume,
          role_suggestions: [
            { role: "Software Engineer", confidence: 90 },
            { role: "Data: Analyst", confidence: 80 },
          ],
        }),
      ),
    );
    let body: Record<string, unknown> | null = null;
    server.use(
      http.post(`${BASE}/jobs/search`, async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({ id: "b2", status: "queued" });
      }),
    );
    const { queryClient } = renderWithProviders(<MatchedJobsPage />);
    await waitFor(() => {
      expect(queryClient.getQueryData(["resume", "latest"])).toBeTruthy();
      expect(queryClient.getQueryData(["portal", "active"])).toBeTruthy();
    });

    await userEvent.selectOptions(screen.getAllByRole("combobox")[0], "Suggested role: Data: Analyst");
    await userEvent.click(screen.getByRole("button", { name: "Find 100" }));

    await waitFor(() => expect(body).not.toBeNull());
    expect(body!.keywords).toBe("Data: Analyst");
    expect(body!.limit).toBe(100);
  });
});

describe("MatchedJobsPage CSV export parity", () => {
  it("exports matched_jobs.csv with the Streamlit column order", async () => {
    let csv = "";
    let downloadName = "";
    // jsdom doesn't implement object URLs and its Blob lacks .text(); patch the object-URL
    // methods (leaving the URL constructor intact so MSW works) and capture the CSV string
    // from the Blob constructor's first part.
    const origCreate = (URL as { createObjectURL?: unknown }).createObjectURL;
    const origRevoke = (URL as { revokeObjectURL?: unknown }).revokeObjectURL;
    URL.createObjectURL = (() => "blob:x") as typeof URL.createObjectURL;
    URL.revokeObjectURL = (() => {}) as typeof URL.revokeObjectURL;
    const RealBlob = globalThis.Blob;
    class CapturingBlob extends RealBlob {
      constructor(parts: BlobPart[], opts?: BlobPropertyBag) {
        super(parts, opts);
        csv = String(parts?.[0] ?? "");
      }
    }
    vi.stubGlobal("Blob", CapturingBlob);
    const clickSpy = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(function (this: HTMLAnchorElement) {
        downloadName = this.download;
      });

    await renderWithJobs([makeJob(1)]);
    await userEvent.click(screen.getByRole("button", { name: /Export matched jobs CSV/ }));

    expect(downloadName).toBe("matched_jobs.csv");
    expect(csv.split("\n")[0]).toBe(
      "Title,Company,Portal,Location,Search Role,Role Priority,Match,Friction,Apply Priority,Job URL",
    );

    clickSpy.mockRestore();
    vi.unstubAllGlobals();
    URL.createObjectURL = origCreate as typeof URL.createObjectURL;
    URL.revokeObjectURL = origRevoke as typeof URL.revokeObjectURL;
  });
});

describe("MatchedJobsPage button-label inventory", () => {
  it("renders the exact Streamlit action labels", async () => {
    await renderWithJobs([makeJob(1)]);
    for (const label of ["Find 20", "Find 50", "Find 100", "Find custom", "Apply Selected Job", "Apply Top 10", "Apply Top 20", "Apply All"]) {
      expect(screen.getByRole("button", { name: label })).toBeInTheDocument();
    }
  });
});
