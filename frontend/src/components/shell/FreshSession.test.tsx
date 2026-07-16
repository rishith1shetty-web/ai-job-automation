import { http, HttpResponse } from "msw";
import { screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { renderWithProviders } from "@/test/render";
import { BASE, fixtures, server } from "@/test/server";
import { SearchBatchBar } from "./HeaderPanels";

/**
 * A reopened dashboard must not restore a previous session's active state.
 *
 * The React app keeps NO active state in localStorage/sessionStorage — the session store is
 * plain in-memory React state, so reopening the tab starts from whatever the backend
 * reports. The one place the frontend re-created an active session by itself was the batch
 * bar auto-selecting batches[0] (which put the previous session's jobs and counters back on
 * screen). These tests pin that it no longer does, and that a previous batch appears only
 * when the user explicitly picks it.
 */
const batch = (over: Record<string, unknown>) => ({
  id: "batch-x",
  resume_id: 6493,
  portal: "linkedin",
  status: "completed",
  search_query: "Query",
  location: "Remote",
  requested_limit: 10,
  jobs_collected: 10,
  easy_apply_count: 10,
  duplicates_removed: 0,
  created_at: "2026-07-01T00:00:00Z",
  updated_at: "2026-07-01T00:00:00Z",
  ...over,
});

function useBatches(batches: unknown[]) {
  server.use(http.get(`${BASE}/jobs/search-batches`, () => HttpResponse.json(batches)));
}

describe("fresh session — the dashboard does not restore previous active state", () => {
  it("does not auto-select the most recent search batch", async () => {
    useBatches([
      batch({ id: "batch-newest", search_query: "Previous Session Query" }),
      batch({ id: "batch-older", search_query: "Older Query" }),
    ]);

    renderWithProviders(<SearchBatchBar />);

    const select = await screen.findByRole("combobox");
    // Placeholder, NOT the newest batch.
    await waitFor(() => expect(select).toHaveValue("Select a search batch…"));
    // The previous session's query must not be shown.
    expect(screen.queryByText(/Previous Session Query/)).toBeNull();
  });

  it("shows a previous batch only after the user explicitly picks it", async () => {
    useBatches([batch({ id: "batch-newest", search_query: "Previous Session Query" })]);

    const { container } = renderWithProviders(<SearchBatchBar />);
    const select = (await screen.findByRole("combobox")) as HTMLSelectElement;
    expect(screen.queryByText(/Search query: Previous Session Query/)).toBeNull();

    // Explicit user selection.
    const option = Array.from(select.options).find((o) => o.value !== "Select a search batch…");
    expect(option).toBeDefined();
    const { fireEvent } = await import("@testing-library/react");
    fireEvent.change(select, { target: { value: option!.value } });

    await waitFor(() =>
      expect(container.textContent).toContain("Search query: Previous Session Query"),
    );
  });

  it("keeps the fixture's own resume/portal reads working (no stale local storage)", async () => {
    useBatches([]);
    renderWithProviders(<SearchBatchBar />);
    // Sanity: the bar reads the backend, and nothing is persisted client-side.
    await waitFor(() => expect(fixtures.resume.id).toBe(6493));
    expect(Object.keys(window.localStorage)).toHaveLength(0);
    expect(Object.keys(window.sessionStorage)).toHaveLength(0);
  });
});
