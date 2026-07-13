import { describe, expect, it } from "vitest";
import { http, HttpResponse } from "msw";
import { screen, waitFor, within } from "@testing-library/react";
import { renderWithProviders } from "@/test/render";
import { BASE, server } from "@/test/server";
import { KpiGrid } from "./KpiGrid";

// A latest apply run mixing every outcome family: 2 successes (applied + the submit-ready
// dry run reached_review), 4 failures, and 1 still-queued job that must NOT be counted.
const batch = {
  id: "batch-1",
  resume_id: 6493,
  portal: "linkedin",
  status: "completed",
  job_results: [
    { status: "applied" },
    { status: "reached_review" },
    { status: "failed" },
    { status: "skipped" },
    { status: "needs_review" },
    { status: "timed_out" },
    { status: "queued" },
  ],
};

function seed(jobResults: unknown, matched = 5) {
  server.use(
    http.get(`${BASE}/applications/apply-top/latest`, () =>
      HttpResponse.json(jobResults === null ? null : { ...batch, job_results: jobResults }),
    ),
    http.get(`${BASE}/dashboard/summary`, () =>
      HttpResponse.json({ matched_jobs: matched, easy_apply_jobs: 0, applied_jobs: 0, needs_review: 0, failed_jobs: 0, skipped_jobs: 0 }),
    ),
  );
}

const cardEl = (label: string) => screen.getByText(label).closest("div.group") as HTMLElement;

describe("KpiGrid — apply-outcome summary cards", () => {
  it("removes the old Easy Apply / Applications / Needs review / Failed-skipped cards", async () => {
    seed(batch.job_results);
    renderWithProviders(<KpiGrid />);
    await screen.findByText("Applications Attempted");

    expect(screen.queryByText("Easy Apply")).toBeNull();
    expect(screen.queryByText("Applications", { exact: true })).toBeNull();
    expect(screen.queryByText("Needs review")).toBeNull();
    expect(screen.queryByText("Failed / skipped")).toBeNull();
  });

  it("renders the new cards with attempted, successful, failed and success-rate values", async () => {
    seed(batch.job_results);
    renderWithProviders(<KpiGrid />);

    // attempted = 6 (queued excluded), successful = 2, failed = 4, rate = round(2/6*100)=33
    await waitFor(() => expect(within(cardEl("Applications Attempted")).getByText("6")).toBeInTheDocument());
    expect(within(cardEl("Successfully Applied")).getByText("2")).toBeInTheDocument();
    expect(within(cardEl("Apply Failed")).getByText("4")).toBeInTheDocument();
    expect(within(cardEl("Success Rate")).getByText("33%")).toBeInTheDocument();
    // Jobs matched stays sourced from the selected search batch summary.
    await waitFor(() => expect(within(cardEl("Jobs matched")).getByText("5")).toBeInTheDocument());
  });

  it("counts a submit-ready dry run (reached_review) as Successfully Applied", async () => {
    seed([{ status: "applied" }, { status: "reached_review" }, { status: "reached_review" }]);
    renderWithProviders(<KpiGrid />);
    await waitFor(() => expect(within(cardEl("Successfully Applied")).getByText("3")).toBeInTheDocument());
    expect(within(cardEl("Success Rate")).getByText("100%")).toBeInTheDocument();
    expect(within(cardEl("Apply Failed")).getByText("0")).toBeInTheDocument();
  });

  it("shows 0% success rate and does not count running/queued jobs when nothing terminal ran", async () => {
    seed([{ status: "running" }, { status: "queued" }]);
    renderWithProviders(<KpiGrid />);
    await waitFor(() => expect(within(cardEl("Applications Attempted")).getByText("0")).toBeInTheDocument());
    expect(within(cardEl("Apply Failed")).getByText("0")).toBeInTheDocument();
    expect(within(cardEl("Success Rate")).getByText("0%")).toBeInTheDocument();
  });

  it("handles no apply run yet (latest batch is null) as all zeros", async () => {
    seed(null);
    renderWithProviders(<KpiGrid />);
    await waitFor(() => expect(within(cardEl("Applications Attempted")).getByText("0")).toBeInTheDocument());
    expect(within(cardEl("Success Rate")).getByText("0%")).toBeInTheDocument();
  });

  it("renders exactly the required 8 cards in order", async () => {
    seed(batch.job_results);
    const { container } = renderWithProviders(<KpiGrid />);
    await screen.findByText("Applications Attempted");

    const order = [
      "Resume", "LinkedIn", "Dice", "Jobs matched",
      "Applications Attempted", "Successfully Applied", "Apply Failed", "Success Rate",
    ];
    const text = container.textContent ?? "";
    let last = -1;
    for (const label of order) {
      const at = text.indexOf(label);
      expect(at, `${label} present and in order`).toBeGreaterThan(last);
      last = at;
    }
    // Exactly 8 KPI cards.
    expect(container.querySelectorAll("div.group").length).toBe(8);
  });
});
