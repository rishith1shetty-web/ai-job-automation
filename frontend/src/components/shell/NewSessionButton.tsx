import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Alert } from "@/components/ui/alert";
import { apiErrorMessage } from "@/lib/apiError";
import { useResetSession, usePortalStatus } from "@/api/portal";
import { useLatestResume } from "@/api/resume";
import { useSearchBatches } from "@/api/jobs";
import { useLatestApplyTopBatch } from "@/api/applications";
import { useSession } from "@/store/session";
import type { BatchStatus } from "@/types";

const CONFIRM_TEXT =
  "This clears the current portal, resume, matches, apply queue and progress from the dashboard. " +
  "Historical data and saved portal login sessions will not be deleted.";

const isActive = (status?: BatchStatus): boolean => status === "queued" || status === "running";

/**
 * "New Session" — resets only the runtime/dashboard session (POST /session/reset) after an
 * explicit confirmation. Historical data and saved portal logins are preserved; the user must
 * re-select a portal, connect, re-analyse a resume and re-run Find Matches afterwards.
 *
 * Disabled while a Find Matches or Apply run is active (or the reset is in flight), so session
 * pointers are never reset out from under a run in progress.
 */
export function NewSessionButton() {
  const [confirming, setConfirming] = useState(false);
  const reset = useResetSession();
  const { resetSession } = useSession();

  const { data: status } = usePortalStatus();
  const { data: resume } = useLatestResume();
  const portal = status?.active_portal ?? undefined;
  const { data: searchBatches } = useSearchBatches(resume?.id, portal);
  const { data: applyBatch } = useLatestApplyTopBatch(resume?.id, portal);

  const runActive =
    isActive(applyBatch?.status) || (searchBatches ?? []).some((b) => isActive(b.status));
  const disabled = runActive || reset.isPending;

  const startNewSession = () => {
    reset.mutate(undefined, {
      onSuccess: () => {
        resetSession(); // drop the selected batch and remount the routed page (local state)
        setConfirming(false);
      },
    });
  };

  return (
    <>
      <Button
        variant="default"
        size="sm"
        disabled={disabled}
        title={runActive ? "Finish or stop the current run before starting a new session." : undefined}
        onClick={() => setConfirming(true)}
      >
        New Session
      </Button>

      {confirming && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
          role="dialog"
          aria-modal="true"
          aria-label="Start a new session"
        >
          <div className="w-full max-w-md rounded-lg border border-parchment-border bg-card p-5 shadow-card">
            <h2 className="text-base font-extrabold text-ink">Start a new session?</h2>
            <p className="mt-2 text-sm text-brown-deep">{CONFIRM_TEXT}</p>
            {reset.isError && (
              <Alert variant="danger" className="mt-3">
                {apiErrorMessage("New Session", reset.error)}
              </Alert>
            )}
            <div className="mt-4 flex justify-end gap-2">
              <Button
                variant="ghost"
                size="sm"
                disabled={reset.isPending}
                onClick={() => setConfirming(false)}
              >
                Cancel
              </Button>
              <Button variant="primary" size="sm" disabled={reset.isPending} onClick={startNewSession}>
                {reset.isPending ? "Starting…" : "Start New Session"}
              </Button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
