// Shared TanStack Query refetchInterval policy for background batches (job search and
// apply-top). Poll every 2s while the batch is queued/running; stop once it reaches a
// terminal state. This mirrors the Streamlit dashboard, which re-reads batch status on
// each rerun only while the batch is still active.
export function pollWhileActive(status: string | undefined | null): number | false {
  // "stopping" keeps polling: the batch is still finishing its current step, and the card
  // must follow it through to "stopped".
  return status === "queued" || status === "running" || status === "stopping" ? 2000 : false;
}
