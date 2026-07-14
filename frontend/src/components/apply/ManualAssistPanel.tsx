import { Hand } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { apiErrorMessage } from "@/lib/apiError";
import {
  useContinueManualAssist,
  useManualAssist,
  useSkipManualAssist,
  type ManualAssistPause,
} from "@/api/manualAssist";

const titleize = (s: string) => s.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());

/** One labelled fact from the pause payload. Rendered generically — nothing here knows or
 *  cares which portal produced it. */
function Fact({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0">
      <div className="text-[.72rem] font-semibold uppercase tracking-[.06em] text-[#8A8580]">{label}</div>
      <div className="mt-0.5 truncate text-sm font-semibold text-ink" title={value}>
        {value || "—"}
      </div>
    </div>
  );
}

/**
 * Manual Input Required — shown while the apply batch is paused on a job whose field
 * automation could not complete. The browser stays open; the user answers that one field
 * and presses Continue Automation.
 *
 * PORTAL-INDEPENDENT: every value comes straight from the backend pause payload. There is no
 * LinkedIn/Dice branch, so a future portal renders correctly with no change here.
 */
export function ManualAssistPanel({
  batchStatus,
  applying,
}: {
  batchStatus?: string | null;
  /** True while a single-job apply is in flight. That path has no batch row to watch, so it is
   *  the only signal that a pause could be about to appear. */
  applying?: boolean;
}) {
  const { data } = useManualAssist(batchStatus, applying);
  const continueAutomation = useContinueManualAssist();
  const skipJob = useSkipManualAssist();

  if (!data?.active) return null;
  const pause = data as ManualAssistPause;

  const pending = continueAutomation.isPending || skipJob.isPending;
  const decision = { batch_id: pause.batch_id, sequence: pause.sequence };
  // A 409 means the pause moved on (stale sequence / wrong batch / already resolved). The
  // mutation already refetched, so the panel below is showing the CURRENT pause — we just
  // explain why the click did nothing. The old request is never replayed.
  const conflict = continueAutomation.error ?? skipJob.error;

  return (
    // The instruction <Alert variant="warn"> below already carries role="alert" +
    // aria-live="assertive", so the panel itself is a labelled region rather than a second
    // live region — otherwise a screen reader announces the pause twice.
    <Card
      role="region"
      aria-label="Manual input required"
      className="border-amber-300 bg-amber-50/60 shadow-[0_2px_10px_rgba(217,119,6,.12)]"
    >
      <CardHeader className="flex-row items-center gap-2 space-y-0">
        <span className="flex size-7 items-center justify-center rounded-full bg-amber-100 text-amber-800" aria-hidden>
          <Hand className="size-4" />
        </span>
        <CardTitle>Manual Input Required</CardTitle>
        <Badge variant="warn" dot>
          {pause.dry_run ? "Dry Run" : "Live Apply"}
        </Badge>
        {pause.attempt > 1 && <Badge variant="muted">Attempt {pause.attempt}</Badge>}
      </CardHeader>

      <CardContent className="space-y-3">
        <Alert variant="warn">
          Complete the highlighted field in the open browser, then click Continue Automation.
        </Alert>

        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          <Fact label="Portal" value={titleize(pause.portal)} />
          <Fact label="Company" value={pause.company} />
          <Fact label="Job title" value={pause.job_title} />
          <Fact label="Current step" value={titleize(pause.current_step)} />
        </div>

        <div className="rounded-lg border border-amber-200 bg-white/70 px-3 py-2">
          <div className="text-[.72rem] font-semibold uppercase tracking-[.06em] text-[#8A8580]">
            Question needing attention
          </div>
          <p className="mt-0.5 text-sm font-semibold text-ink">{pause.question || "—"}</p>
          <p className="mt-1 text-sm text-[#6B6459]">{pause.reason}</p>
        </div>

        {conflict && <Alert variant="danger">{apiErrorMessage("Manual assist", conflict)}</Alert>}

        <div className="flex flex-wrap gap-2">
          <Button
            variant="primary"
            disabled={pending}
            onClick={() => continueAutomation.mutate(decision)}
          >
            Continue Automation
          </Button>
          <Button variant="default" disabled={pending} onClick={() => skipJob.mutate(decision)}>
            Skip This Job
          </Button>
          {pending && <span className="self-center text-sm text-[#8A8580]">Working…</span>}
        </div>
      </CardContent>
    </Card>
  );
}
