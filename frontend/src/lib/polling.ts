// Shared TanStack Query refetchInterval policy for background batches (job search and
// apply-top). Poll every 2s while the batch is still active; stop once it reaches a
// terminal state. This mirrors the Streamlit dashboard, which re-reads batch status on
// each rerun only while the batch is still active.
//
// "paused" is active, not terminal: an apply batch parked on a job that needs manual input
// is still running and will resume, so the status card must keep polling to see it move.
const ACTIVE_BATCH_STATUSES = new Set(["queued", "running", "paused"]);

export function pollWhileActive(status: string | undefined | null): number | false {
  return status != null && ACTIVE_BATCH_STATUSES.has(status) ? 2000 : false;
}
