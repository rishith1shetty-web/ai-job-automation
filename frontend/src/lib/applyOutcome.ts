// Centralized classifier for apply-run outcomes, used by the top KPI summary cards.
//
// The status strings come straight from the backend's apply-top batch results
// (app/main.py `_batch_job_status` / `_apply_top_failure_result`). Each processed job is
// recorded in `job_results[].status` with one of these terminal values:
//   applied         – live submission completed
//   reached_review  – dry run reached the submit/review-ready stage (DRY_RUN_READY, or a
//                     SKIPPED job whose current_step was ready_to_submit/review_reached)
//   failed | skipped | needs_review | timed_out – unsuccessful terminal outcomes
// (`needs_review` also absorbs `unsupported_form` upstream.)
//
// We must classify per-job rather than reuse the batch's aggregate counts, because the
// backend buckets the SUCCESS status `reached_review` inside `skipped_count`, so those
// totals cannot separate success from failure.

export type ApplyOutcome = "success" | "failed" | "pending";

// Exact current success statuses: applied + reached_review. The extra synonyms
// (submitted/dry_run_ready/submit_ready) are defensive — semantically identical outcomes
// the backend could emit in the future — and never hurt the current mapping.
export const APPLY_SUCCESS_STATUSES = new Set([
  "applied",
  "submitted",
  "reached_review",
  "dry_run_ready",
  "submit_ready",
]);

// Non-terminal states are never counted as attempted (success or failure).
export const APPLY_PENDING_STATUSES = new Set([
  "",
  "queued",
  "running",
  "matched",
  "pending",
  "in_progress",
]);

/** Classify a single apply-run job status. Anything terminal that isn't a known success
 *  (and isn't still queued/running) is a failure — regardless of the specific reason. */
export function classifyApplyStatus(status: unknown): ApplyOutcome {
  const s = (typeof status === "string" ? status : "").trim().toLowerCase();
  if (APPLY_SUCCESS_STATUSES.has(s)) return "success";
  if (APPLY_PENDING_STATUSES.has(s)) return "pending";
  return "failed";
}

export interface ApplyRunSummary {
  attempted: number;
  successful: number;
  failed: number;
  /** Whole-number percentage (0 when nothing has been attempted). */
  successRate: number;
}

/** Summarise the current/latest apply run from its per-job results.
 *  attempted = successful + failed ; failed = attempted − successful (each unsuccessful
 *  terminal job counted exactly once) ; successRate = successful / attempted × 100. */
export function summarizeApplyRun(
  jobResults: ReadonlyArray<{ status?: unknown }> | undefined | null,
): ApplyRunSummary {
  let attempted = 0;
  let successful = 0;
  for (const result of jobResults ?? []) {
    const outcome = classifyApplyStatus(result?.status);
    if (outcome === "pending") continue; // queued/running not counted either way
    attempted += 1;
    if (outcome === "success") successful += 1;
  }
  const failed = attempted - successful;
  const successRate = attempted > 0 ? Math.round((successful / attempted) * 100) : 0;
  return { attempted, successful, failed, successRate };
}
