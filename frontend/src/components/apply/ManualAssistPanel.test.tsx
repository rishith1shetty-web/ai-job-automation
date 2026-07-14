import { describe, expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderWithProviders } from "@/test/render";
import { BASE, server } from "@/test/server";
import { ManualAssistPanel } from "./ManualAssistPanel";

const pause = {
  active: true,
  batch_id: "batch-9",
  portal: "linkedin",
  job_id: 42,
  job_title: "Senior Data Analyst",
  company: "Acme Corp",
  job_url: "https://linkedin.com/jobs/view/42",
  current_step: "manual_input_required",
  question: "Years of experience with Kubernetes?",
  reason: "Automation could not answer this screening question.",
  dry_run: true,
  attempt: 1,
  sequence: 3,
  paused_at: "2026-07-13T10:00:00Z",
};

/** Serve a sequence of GET responses, one per call (so a re-pause can follow a Continue). */
function seedState(...states: unknown[]) {
  let i = 0;
  server.use(
    http.get(`${BASE}/applications/apply-top/manual-assist`, () => {
      const body = states[Math.min(i, states.length - 1)];
      i += 1;
      return HttpResponse.json(body);
    }),
  );
}

function captureDecision(path: "continue" | "skip", status = 200, body: unknown = { accepted: true }) {
  const seen: unknown[] = [];
  server.use(
    http.post(`${BASE}/applications/apply-top/manual-assist/${path}`, async ({ request }) => {
      seen.push(await request.json());
      return HttpResponse.json(body, { status });
    }),
  );
  return seen;
}

describe("ManualAssistPanel", () => {
  it("renders nothing when no pause is active", async () => {
    seedState({ active: false });
    renderWithProviders(<ManualAssistPanel batchStatus="running" />);
    await waitFor(() => expect(screen.queryByRole("region", { name: "Manual input required" })).toBeNull());
    expect(screen.queryByRole("heading", { name: "Manual Input Required" })).toBeNull();
  });

  it("shows the panel with portal, company, job, step, question and reason", async () => {
    seedState(pause);
    renderWithProviders(<ManualAssistPanel batchStatus="paused" />);

    expect(await screen.findByRole("heading", { name: "Manual Input Required" })).toBeInTheDocument();
    // Portal is titleized generically (no per-portal lookup table — that would be branching).
    expect(screen.getByText("Linkedin")).toBeInTheDocument();
    expect(screen.getByText("Acme Corp")).toBeInTheDocument();
    expect(screen.getByText("Senior Data Analyst")).toBeInTheDocument();
    // "Current step" renders the backend value manual_input_required, titleized.
    expect(screen.getByTitle("Manual Input Required")).toBeInTheDocument();
    expect(screen.getByText("Years of experience with Kubernetes?")).toBeInTheDocument();
    expect(screen.getByText(pause.reason)).toBeInTheDocument();
    expect(
      screen.getByText("Complete the highlighted field in the open browser, then click Continue Automation."),
    ).toBeInTheDocument();
    // Landmark region for the panel, plus a single assertive live region (the instruction).
    expect(screen.getByRole("region", { name: "Manual input required" })).toBeInTheDocument();
    expect(screen.getByRole("alert")).toBeInTheDocument();
  });

  it("renders any portal the backend reports without a frontend change", async () => {
    seedState({ ...pause, portal: "indeed" });   // a portal React has never heard of
    renderWithProviders(<ManualAssistPanel batchStatus="paused" />);
    expect(await screen.findByText("Indeed")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Manual Input Required" })).toBeInTheDocument();
  });

  it("labels Dry Run and Live Apply from the payload", async () => {
    seedState(pause);
    const { unmount } = renderWithProviders(<ManualAssistPanel batchStatus="paused" />);
    expect(await screen.findByText("Dry Run")).toBeInTheDocument();
    unmount();

    seedState({ ...pause, dry_run: false });
    renderWithProviders(<ManualAssistPanel batchStatus="paused" />);
    expect(await screen.findByText("Live Apply")).toBeInTheDocument();
  });

  it("Continue sends the exact batch_id and sequence", async () => {
    const user = userEvent.setup();
    seedState(pause);
    const sent = captureDecision("continue");

    renderWithProviders(<ManualAssistPanel batchStatus="paused" />);
    await user.click(await screen.findByRole("button", { name: "Continue Automation" }));

    await waitFor(() => expect(sent).toEqual([{ batch_id: "batch-9", sequence: 3 }]));
  });

  it("Skip sends the exact batch_id and sequence", async () => {
    const user = userEvent.setup();
    seedState(pause);
    const sent = captureDecision("skip");

    renderWithProviders(<ManualAssistPanel batchStatus="paused" />);
    await user.click(await screen.findByRole("button", { name: "Skip This Job" }));

    await waitFor(() => expect(sent).toEqual([{ batch_id: "batch-9", sequence: 3 }]));
  });

  it("disables both buttons while a decision is pending", async () => {
    const user = userEvent.setup();
    seedState(pause);
    server.use(
      http.post(`${BASE}/applications/apply-top/manual-assist/continue`, async () => {
        await new Promise((r) => setTimeout(r, 80));
        return HttpResponse.json({ accepted: true });
      }),
    );

    renderWithProviders(<ManualAssistPanel batchStatus="paused" />);
    const cont = await screen.findByRole("button", { name: "Continue Automation" });
    await user.click(cont);

    await waitFor(() => expect(cont).toBeDisabled());
    expect(screen.getByRole("button", { name: "Skip This Job" })).toBeDisabled();
  });

  it("keeps the panel open and shows the updated reason when re-validation fails", async () => {
    const user = userEvent.setup();
    const repaused = {
      ...pause,
      sequence: 4,
      attempt: 2,
      reason: "This question still looks incomplete. Please finish it in the open browser.",
    };
    seedState(pause, repaused);          // Continue -> backend re-pauses with a NEWER sequence
    const sent = captureDecision("continue");

    renderWithProviders(<ManualAssistPanel batchStatus="paused" />);
    await user.click(await screen.findByRole("button", { name: "Continue Automation" }));

    // Panel stays up, shows the new reason and attempt — and never claims the job completed.
    expect(await screen.findByText(repaused.reason)).toBeInTheDocument();
    expect(screen.getByText("Attempt 2")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Manual Input Required" })).toBeInTheDocument();
    expect(screen.queryByText(/completed successfully/i)).toBeNull();
    expect(sent).toHaveLength(1);        // the old request was NOT replayed
  });

  it("clears the panel once the backend resolves the pause", async () => {
    const user = userEvent.setup();
    seedState(pause, { active: false });
    captureDecision("skip");

    renderWithProviders(<ManualAssistPanel batchStatus="paused" />);
    await user.click(await screen.findByRole("button", { name: "Skip This Job" }));

    await waitFor(() => expect(screen.queryByRole("heading", { name: "Manual Input Required" })).toBeNull());
  });

  it("on a stale-sequence 409 it refetches, shows the newest pause and does not resend", async () => {
    const user = userEvent.setup();
    const newer = { ...pause, sequence: 5, attempt: 2, reason: "A newer pause is waiting." };
    seedState(pause, newer);
    const sent = captureDecision("continue", 409, { detail: "This pause has already been resolved." });

    renderWithProviders(<ManualAssistPanel batchStatus="paused" />);
    await user.click(await screen.findByRole("button", { name: "Continue Automation" }));

    // The 409 is surfaced in the existing API-error style...
    expect(await screen.findByText(/already been resolved/i)).toBeInTheDocument();
    // ...the panel now shows the CURRENT pause...
    expect(await screen.findByText("A newer pause is waiting.")).toBeInTheDocument();
    // ...and the stale request is never retried automatically.
    expect(sent).toHaveLength(1);
  });

  it("does not poll when no apply batch is active", async () => {
    vi.useFakeTimers();
    let calls = 0;
    server.use(
      http.get(`${BASE}/applications/apply-top/manual-assist`, () => {
        calls += 1;
        return HttpResponse.json({ active: false });
      }),
    );
    renderWithProviders(<ManualAssistPanel batchStatus={undefined} />);
    await vi.advanceTimersByTimeAsync(10_000);
    vi.useRealTimers();
    expect(calls).toBeLessThanOrEqual(1);   // bounded: fetched once, never polled
  });
});
