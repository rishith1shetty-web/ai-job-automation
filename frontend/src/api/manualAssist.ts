import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiRequest } from "@/lib/api";
import { pollWhileActive } from "@/lib/polling";

// Manual assist — PORTAL-INDEPENDENT. React renders whatever the backend reports and never
// branches on the portal, so LinkedIn, Dice and any future portal (Indeed, Naukri, …) work
// with no frontend change. That is why `portal` is a plain string, not a union.
//   GET  /applications/apply-top/manual-assist
//   POST /applications/apply-top/manual-assist/continue  { batch_id, sequence }
//   POST /applications/apply-top/manual-assist/skip      { batch_id, sequence }

export interface ManualAssistPause {
  active: true;
  batch_id: string | null;
  portal: string;
  job_id: number;
  job_title: string;
  company: string;
  job_url: string;
  current_step: string;
  question: string;
  reason: string;
  dry_run: boolean;
  attempt: number;
  /** Bumped on every pause. Sent back on Continue/Skip so a stale click 409s instead of
   *  resolving a newer pause the user has not read yet. */
  sequence: number;
  paused_at: string;
}

export type ManualAssistState = { active: false } | ManualAssistPause;

export const MANUAL_ASSIST_KEY = ["manual-assist"] as const;

/**
 * Poll the pause state. Reuses the project's existing bounded policy (`pollWhileActive`):
 *  - while paused              -> every 2s (so a re-pause with a new reason shows up promptly)
 *  - while a batch is live     -> every 2s (a pause can appear at any moment)
 *  - while a single apply runs -> every 2s (apply-selected has NO batch row to watch, and its
 *                                 request stays open for as long as automation is paused, so
 *                                 without this the pause would never become visible)
 *  - otherwise                 -> not polled at all
 */
export function useManualAssist(batchStatus?: string | null, applying?: boolean) {
  return useQuery({
    queryKey: MANUAL_ASSIST_KEY,
    queryFn: () => apiRequest<ManualAssistState>("GET", "/applications/apply-top/manual-assist"),
    refetchInterval: (query) =>
      query.state.data?.active || applying ? 2000 : pollWhileActive(batchStatus),
  });
}

export interface ManualAssistDecisionInput {
  batch_id: string | null;
  sequence: number;
}

function useManualAssistDecision(path: "continue" | "skip") {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: ManualAssistDecisionInput) =>
      apiRequest<{ accepted: boolean; decision: string; sequence: number }>(
        "POST",
        `/applications/apply-top/manual-assist/${path}`,
        { body: input },
      ),
    // Re-read the pause either way. On success the backend may have cleared it, or (when
    // re-validation failed) re-paused with a NEWER sequence and an updated reason. On a 409
    // (stale sequence / wrong batch / nothing paused) the refetch pulls the current truth —
    // the old request is never replayed.
    onSettled: () => queryClient.invalidateQueries({ queryKey: MANUAL_ASSIST_KEY }),
  });
}

export const useContinueManualAssist = () => useManualAssistDecision("continue");
export const useSkipManualAssist = () => useManualAssistDecision("skip");
