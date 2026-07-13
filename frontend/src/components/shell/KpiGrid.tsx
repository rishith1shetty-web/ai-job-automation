import { FileText, Linkedin, Dices, Target, Send, CheckCircle2, XCircle, Percent } from "lucide-react";
import { cn } from "@/lib/utils";
import { Skeleton } from "@/components/ui/skeleton";
import { usePortalStatus } from "@/api/portal";
import { useLatestResume } from "@/api/resume";
import { useDashboardSummary } from "@/api/jobs";
import { useLatestApplyTopBatch } from "@/api/applications";
import { summarizeApplyRun } from "@/lib/applyOutcome";
import { useSession } from "@/store/session";

type Tone = "" | "good" | "warn" | "muted";

interface Kpi {
  label: string;
  value: React.ReactNode;
  icon: React.ComponentType<{ className?: string }>;
  tone: Tone;
  note?: string;
  /** Optional tooltip (native title) — used for the Successfully Applied helper text. */
  title?: string;
}

function KpiCard({ kpi }: { kpi: Kpi }) {
  const Icon = kpi.icon;
  return (
    <div
      title={kpi.title}
      className="group relative flex min-h-[104px] animate-fade-in flex-col overflow-hidden rounded-lg border border-parchment-border bg-card px-4 py-3.5 shadow-card transition-transform duration-200 hover:-translate-y-0.5 hover:shadow-lift"
    >
      <div
        className="absolute inset-x-0 top-0 h-[3px] bg-gradient-to-r from-gold to-gold-deep opacity-0 transition-opacity group-hover:opacity-100"
        aria-hidden
      />
      <div className="flex items-center justify-between">
        <span className="text-[.72rem] font-bold uppercase tracking-[.06em] text-[#8A8580]">{kpi.label}</span>
        <span
          className={cn(
            "flex size-8 items-center justify-center rounded-[9px]",
            kpi.tone === "good"
              ? "bg-green-50 text-green-700"
              : kpi.tone === "warn"
                ? "bg-red-50 text-destructive"
                : "bg-parchment-amber text-gold-text",
          )}
        >
          <Icon className="size-4" />
        </span>
      </div>
      <div
        className={cn(
          "mt-1 text-2xl font-extrabold tabular-nums leading-tight",
          kpi.tone === "good" ? "text-green-700" : kpi.tone === "muted" ? "text-[#8A8580]" : "text-ink",
        )}
      >
        {kpi.value}
      </div>
      {kpi.note && <div className="mt-0.5 truncate text-xs text-[#8A8580]">{kpi.note}</div>}
    </div>
  );
}

export function KpiGrid() {
  const { data: status } = usePortalStatus(); // LIVE
  const { data: resume } = useLatestResume(); // LIVE
  const { selectedBatchId } = useSession();
  const { data: summary, isLoading: summaryLoading } = useDashboardSummary(
    resume?.id,
    selectedBatchId,
    status?.active_portal,
  ); // LIVE — used only for Jobs matched (the selected search batch)
  // Current/latest apply run for this resume + active portal (existing endpoint the
  // dashboard already reads). Row 2 is derived from its per-job results.
  const { data: latestBatch, isLoading: batchLoading } = useLatestApplyTopBatch(resume?.id, status?.active_portal);

  const linkedinConnected = Boolean(status?.linkedin_connected);
  const diceConnected = Boolean(status?.dice_connected);
  const matchedJobs = summary?.matched_jobs ?? 0;

  // attempted = successful + failed ; failed = attempted − successful ; rate = s/a×100.
  // (Aggregate batch counts can't be used: the backend files the success status
  // "reached_review" under skipped_count, so we classify each job_result instead.)
  const run = summarizeApplyRun(latestBatch?.job_results);

  // Shimmer in place of a value while its source query is still loading.
  const matched = (v: React.ReactNode) => (summaryLoading ? <Skeleton className="mt-1 h-6 w-12 border-0" /> : v);
  const runValue = (v: React.ReactNode) => (batchLoading ? <Skeleton className="mt-1 h-6 w-12 border-0" /> : v);

  const cards: Kpi[] = [
    // Row 1 — unchanged
    { label: "Resume", icon: FileText, tone: resume ? "good" : "muted", value: resume ? "Ready" : "Not loaded", note: resume?.filename },
    { label: "LinkedIn", icon: Linkedin, tone: linkedinConnected ? "good" : "muted", value: linkedinConnected ? "Connected" : "Offline" },
    { label: "Dice", icon: Dices, tone: diceConnected ? "good" : "muted", value: diceConnected ? "Connected" : "Offline" },
    { label: "Jobs matched", icon: Target, tone: "", value: matched(matchedJobs) },
    // Row 2 — outcomes of the current/latest apply run
    { label: "Applications Attempted", icon: Send, tone: "", value: runValue(run.attempted) },
    {
      label: "Successfully Applied",
      icon: CheckCircle2,
      tone: "good",
      value: runValue(run.successful),
      title: "Includes live submissions and dry runs that successfully reached the submit-ready stage.",
    },
    { label: "Apply Failed", icon: XCircle, tone: run.failed ? "warn" : "muted", value: runValue(run.failed) },
    { label: "Success Rate", icon: Percent, tone: "", value: runValue(`${run.successRate}%`) },
  ];
  return (
    <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
      {cards.map((kpi) => (
        <KpiCard key={kpi.label} kpi={kpi} />
      ))}
    </div>
  );
}
