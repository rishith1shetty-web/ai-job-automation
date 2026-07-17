import { http, HttpResponse } from "msw";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { renderWithProviders } from "@/test/render";
import { BASE, server } from "@/test/server";
import { NewSessionButton } from "./NewSessionButton";

// Default handlers already answer /portal/active, /resumes/latest and
// /applications/apply-top/latest. Search batches has no default handler, so each test seeds it.
function seedRuns(opts: { search?: unknown[]; apply?: unknown } = {}) {
  server.use(
    http.get(`${BASE}/jobs/search-batches`, () => HttpResponse.json(opts.search ?? [])),
    http.get(`${BASE}/applications/apply-top/latest`, () => HttpResponse.json(opts.apply ?? null)),
  );
}

const CONFIRM =
  "This clears the current portal, resume, matches, apply queue and progress from the dashboard. " +
  "Historical data and saved portal login sessions will not be deleted.";

describe("NewSessionButton", () => {
  it("shows the confirmation dialog with the required wording", async () => {
    seedRuns();
    renderWithProviders(<NewSessionButton />);
    await userEvent.click(await screen.findByRole("button", { name: "New Session" }));
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getByText(CONFIRM)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start New Session" })).toBeInTheDocument();
  });

  it("Cancel makes no request and closes the dialog", async () => {
    seedRuns();
    let posted = false;
    server.use(
      http.post(`${BASE}/session/reset`, () => {
        posted = true;
        return HttpResponse.json({});
      }),
    );
    renderWithProviders(<NewSessionButton />);
    await userEvent.click(await screen.findByRole("button", { name: "New Session" }));
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(posted).toBe(false);
  });

  it("Start New Session POSTs /session/reset and closes the dialog", async () => {
    seedRuns();
    let posted = false;
    server.use(
      http.post(`${BASE}/session/reset`, () => {
        posted = true;
        return HttpResponse.json({
          active_portal: null,
          linkedin_connected: false,
          dice_connected: false,
          active_resume_id: null,
          active_search_batch_id: null,
          current_matches: [],
          current_apply_queue: [],
          current_progress: { total: 0, completed: 0, applied: 0, failed: 0, skipped: 0, needs_review: 0 },
          current_run_status: "idle",
          current_run_error: null,
        });
      }),
    );
    renderWithProviders(<NewSessionButton />);
    await userEvent.click(await screen.findByRole("button", { name: "New Session" }));
    await userEvent.click(screen.getByRole("button", { name: "Start New Session" }));
    await waitFor(() => expect(posted).toBe(true));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("is disabled while an apply run is active", async () => {
    seedRuns({ apply: { id: "b1", status: "running" } });
    renderWithProviders(<NewSessionButton />);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "New Session" })).toBeDisabled(),
    );
  });

  it("is disabled while a search run is active", async () => {
    seedRuns({ search: [{ id: "s1", status: "running", resume_id: 6493, portal: "linkedin" }] });
    renderWithProviders(<NewSessionButton />);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "New Session" })).toBeDisabled(),
    );
  });

  it("is enabled when no run is active", async () => {
    seedRuns({
      search: [{ id: "s1", status: "completed", resume_id: 6493, portal: "linkedin" }],
      apply: { id: "b1", status: "completed" },
    });
    renderWithProviders(<NewSessionButton />);
    // First it must have loaded the (completed) run states, then stay enabled.
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "New Session" })).toBeEnabled(),
    );
    await userEvent.click(screen.getByRole("button", { name: "New Session" }));
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });
});
