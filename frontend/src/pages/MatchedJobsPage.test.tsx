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
  it("defaults the Dry run toggle to OFF", async () => {
    await renderWithJobs([makeJob(1)]);
    expect(screen.getByRole("switch", { name: "Dry run" })).toHaveAttribute("aria-checked", "false");
  });

  it("keeps the Dry run toggle OFF after a remount (app start / New Session)", async () => {
    // The toggle is plain component state with no browser storage behind it, and New Session
    // remounts this page, so a previously chosen ON can never come back on its own.
    const first = await renderWithJobs([makeJob(1)]);
    await userEvent.click(screen.getByRole("switch", { name: "Dry run" }));
    expect(screen.getByRole("switch", { name: "Dry run" })).toHaveAttribute("aria-checked", "true");
    first.unmount();

    await renderWithJobs([makeJob(1)]);
    expect(screen.getByRole("switch", { name: "Dry run" })).toHaveAttribute("aria-checked", "false");
  });

  it("renders rows in the backend's order and shows each job's posted age", async () => {
    // The dashboard must mirror the backend's freshness-aware ranking exactly: no client-side
    // re-sorting, and the portal's raw posted text is displayed per row ("Unknown" when absent).
    await renderWithJobs([
      makeJob(2, { job_description_metadata: { posted_date: "7 minutes ago", posted_at: "2026-07-17T14:23:00" } }),
      makeJob(1, { job_description_metadata: { posted_date: "3 days ago", posted_at: "2026-07-14T14:30:00" } }),
      makeJob(3), // no posted metadata -> unknown
    ]);
    const cells = screen.getAllByRole("cell").map((cell) => cell.textContent);
    const titleOrder = ["Job 2", "Job 1", "Job 3"].map((title) => cells.indexOf(title));
    expect(titleOrder).toEqual([...titleOrder].sort((a, b) => a - b)); // backend order preserved
    expect(cells).toContain("7 minutes ago");
    expect(cells).toContain("3 days ago");
    expect(cells).toContain("Unknown");
  });

  it("preserves the backend's variation-grouped order exactly (no client regroup/sort)", async () => {
    // Backend returns jobs already grouped by variation (var1 newest-first, then var2, ...).
    // The table must render that exact order -- a newer variation-2 job must NOT jump above
    // variation-1 jobs on the client.
    await renderWithJobs([
      makeJob(1, { job_description_metadata: { posted_display: "7 minutes ago", posted_at: "2026-07-17T14:23:00", variation_index: 1 } }),
      makeJob(2, { job_description_metadata: { posted_display: "1 hour ago", posted_at: "2026-07-17T13:30:00", variation_index: 1 } }),
      makeJob(3, { job_description_metadata: { posted_display: "1 minute ago", posted_at: "2026-07-17T14:29:00", variation_index: 2 } }),
    ]);
    // Job 3 is the newest by time but belongs to variation 2, so it must stay LAST -- the
    // client renders the backend's grouped order verbatim, never re-sorting by time.
    const cells = screen.getAllByRole("cell").map((cell) => cell.textContent);
    const order = ["Job 1", "Job 2", "Job 3"].map((title) => cells.indexOf(title));
    expect(order).toEqual([...order].sort((a, b) => a - b)); // exact backend order preserved
  });

  it("shows only the intended columns — Match and Friction are removed", async () => {
    await renderWithJobs([makeJob(1, { job_description_metadata: { posted_date: "5 min ago" } })]);
    const headers = screen.getAllByRole("columnheader").map((h) => h.textContent);
    expect(headers).toEqual([
      "Title", "Company", "Portal", "Posted", "Apply Priority", "Easy Apply", "Status", "Job URL",
    ]);
    // The removed cells' bar/score renderings are gone: match rendered as its own "90.0%"
    // text node and friction as "10.0" -- neither may appear anywhere now. (The job radio
    // label embeds the match % inside one longer string, which exact matching ignores.)
    expect(screen.queryByText("90.0%")).toBeNull();
    expect(screen.queryByText("10.0")).toBeNull();
    // Every row has exactly one cell per remaining header, and Posted styling is intact.
    expect(screen.getAllByRole("cell")).toHaveLength(headers.length);
    expect(
      screen.getByText("5 min ago").closest("[data-posted-tier]")?.getAttribute("data-posted-tier"),
    ).toBe("minutes");
  });

  it("gives posted ages tiered visual emphasis without altering text or order", async () => {
    // Display-only: recent ages get emphasized Badge presentation, older ages are neutral,
    // unknown is muted -- while the raw text and the backend row order stay untouched.
    await renderWithJobs([
      makeJob(1, { job_description_metadata: { posted_date: "Just now" } }),
      makeJob(2, { job_description_metadata: { posted_date: "5 min ago" } }),
      makeJob(3, { job_description_metadata: { posted_date: "2 hours ago" } }),
      makeJob(4, { job_description_metadata: { posted_date: "Today" } }),
      makeJob(5, { job_description_metadata: { posted_date: "3 days ago" } }),
      makeJob(6), // unknown
    ]);
    const tierOf = (text: string) =>
      screen.getByText(text).closest("[data-posted-tier]")?.getAttribute("data-posted-tier");
    expect(tierOf("Just now")).toBe("instant");
    expect(tierOf("5 min ago")).toBe("minutes");
    expect(tierOf("2 hours ago")).toBe("hours");
    expect(tierOf("Today")).toBe("today");
    expect(tierOf("3 days ago")).toBe("older");
    expect(tierOf("Unknown")).toBe("unknown");
    // Backend order still intact with the styling applied.
    const cells = screen.getAllByRole("cell").map((cell) => cell.textContent);
    const titleOrder = [1, 2, 3, 4, 5, 6].map((i) => cells.indexOf(`Job ${i}`));
    expect(titleOrder).toEqual([...titleOrder].sort((a, b) => a - b));
  });

  it("shows the Dice posted_display relative age, never the raw ISO or stale badge; LinkedIn text unchanged", async () => {
    // Dice backend supplies posted_display (derived from the exact posted_at); posted_date
    // holds the coarse badge or raw ISO for diagnostics and must never reach the cell.
    // LinkedIn has no posted_display -> its own relative text is shown verbatim (unchanged).
    await renderWithJobs([
      makeJob(1, { portal: "dice", job_description_metadata: {
        posted_date: "Today", posted_at: "2026-07-15T09:00:00", posted_display: "2 days ago" } }),
      makeJob(2, { portal: "dice", job_description_metadata: {
        posted_date: "2026-07-17T13:36:16Z", posted_at: "2026-07-17T13:36:16", posted_display: "7 minutes ago" } }),
      makeJob(3, { portal: "linkedin", job_description_metadata: {
        posted_date: "Reposted 2 weeks ago", posted_at: "2026-07-03T14:30:00" } }),
    ]);
    const cells = screen.getAllByRole("cell").map((cell) => cell.textContent);
    expect(cells).toContain("2 days ago");     // Dice: relative posted_display, not "Today"
    expect(cells).toContain("7 minutes ago");  // Dice: relative posted_display, not the ISO
    expect(cells).not.toContain("Today");      // stale badge is never displayed
    expect(cells.some((c) => c !== null && /\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(c))).toBe(false);
    expect(cells).toContain("Reposted 2 weeks ago"); // LinkedIn: raw relative text preserved
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
    expect(body!).toEqual({ resume_id: 6493, job_id: 1, dry_run: false, screening_mode: "safe" });
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
    expect(body!.dry_run).toBe(false);
    expect(body!.screening_mode).toBe("safe");
    expect(body!.search_batch_id).toBeNull();
  });

  it("Apply All sends every eligible job (min(job_count, 100))", async () => {
    // 60 jobs -> Apply All processes all 60 in one batch (the old cap was 50).
    let body: Record<string, unknown> | null = null;
    server.use(
      http.post(`${BASE}/applications/apply-top/start`, async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(applyBatch);
      }),
    );
    await renderWithJobs(Array.from({ length: 60 }, (_, i) => makeJob(i + 1)));
    // No cap note below 100.
    expect(screen.queryByText(/Apply All processes up to 100/)).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Apply All" }));
    await waitFor(() => expect(body).not.toBeNull());
    expect(body!.limit).toBe(60);
  });

  it("Apply All is capped at 100 when more than 100 jobs are matched", async () => {
    let body: Record<string, unknown> | null = null;
    server.use(
      http.post(`${BASE}/applications/apply-top/start`, async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(applyBatch);
      }),
    );
    await renderWithJobs(Array.from({ length: 101 }, (_, i) => makeJob(i + 1)));
    expect(screen.getByText("Apply All processes up to 100 ranked jobs per batch.")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Apply All" }));
    await waitFor(() => expect(body).not.toBeNull());
    expect(body!.limit).toBe(100);
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

describe("MatchedJobsPage Find Matches buttons", () => {
  async function renderReady() {
    const view = renderWithProviders(<MatchedJobsPage />);
    await waitFor(() => {
      expect(view.queryClient.getQueryData(["resume", "latest"])).toBeTruthy();
      expect(view.queryClient.getQueryData(["portal", "active"])).toBeTruthy();
    });
    return view;
  }

  function captureSearchBodies() {
    const bodies: Record<string, unknown>[] = [];
    server.use(
      http.post(`${BASE}/jobs/search`, async ({ request }) => {
        bodies.push((await request.json()) as Record<string, unknown>);
        return HttpResponse.json({ id: `b${bodies.length}`, status: "queued" });
      }),
    );
    return bodies;
  }

  it("sends the requested limit for Find 20, Find 50, Find 100 and Find custom", async () => {
    const bodies = captureSearchBodies();
    await renderReady();

    for (const [name, limit] of [["Find 20", 20], ["Find 50", 50], ["Find 100", 100]] as const) {
      await userEvent.click(screen.getByRole("button", { name }));
      await waitFor(() => expect(bodies.at(-1)?.limit).toBe(limit));
      await waitFor(() => expect(screen.getByRole("button", { name })).toBeEnabled());
    }

    const custom = screen.getByRole("spinbutton");
    await userEvent.clear(custom);
    await userEvent.type(custom, "35");
    await userEvent.click(screen.getByRole("button", { name: "Find custom" }));
    await waitFor(() => expect(bodies.at(-1)?.limit).toBe(35));
    expect(bodies.map((b) => b.portal)).toEqual(["linkedin", "linkedin", "linkedin", "linkedin"]);
  });

  it("does not look clickable when no portal is active, and says what to do", async () => {
    // Every backend start resets the session to active_portal: null. The buttons used to stay
    // enabled and silently do nothing; they must be disabled with a visible explanation.
    const bodies = captureSearchBodies();
    server.use(
      http.get(`${BASE}/portal/active`, () =>
        HttpResponse.json({ active_portal: null, linkedin_connected: false, dice_connected: false }),
      ),
    );
    await renderReady();

    for (const name of ["Find 20", "Find 50", "Find 100", "Find custom"]) {
      expect(screen.getByRole("button", { name })).toBeDisabled();
    }
    expect(screen.getByText(/Choose LinkedIn or Dice in the sidebar/i)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Find 20" }));
    expect(bodies).toEqual([]);
  });

  it("shows the backend's reason when the search request is refused", async () => {
    server.use(
      http.post(`${BASE}/jobs/search`, () =>
        HttpResponse.json(
          { detail: "Dice is the active search portal, but the saved Dice session is missing or not verified." },
          { status: 409 },
        ),
      ),
    );
    await renderReady();

    await userEvent.click(screen.getByRole("button", { name: "Find 20" }));
    expect(await screen.findByText(/saved Dice session is missing or not verified/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Find 20" })).toBeEnabled();
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
