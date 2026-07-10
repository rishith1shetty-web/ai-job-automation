import { FileText, Linkedin, Dices, Target, Zap, Send, Eye, TriangleAlert } from "lucide-react";
import { cn } from "@/lib/utils";
import { usePortalStatus } from "@/api/portal";
import { useLatestResume } from "@/api/resume";
import { useDashboardSummary } from "@/api/jobs";
import { useSession } from "@/store/session";

type Tone = "" | "good" | "warn" | "muted";

interface Kpi {
  label: string;
  value: React.ReactNode;
  icon: React.ComponentType<{ className?: string }>;
  tone: Tone;
  note?: string;
}

function KpiCard({ kpi }: { kpi: Kpi }) {
  const Icon = kpi.icon;
  return (
    <div className="group relative animate-fade-in overflow-hidden rounded-lg border border-parchment-border bg-card px-4 py-3.5 shadow-card transition-transform duration-200 hover:-translate-y-0.5 hover:shadow-lift">
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
  const { data: summary } = useDashboardSummary(resume?.id, selectedBatchId, status?.active_portal); // LIVE

  const linkedinConnected = Boolean(status?.linkedin_connected);
  const diceConnected = Boolean(status?.dice_connected);
  const s = summary ?? {
    matched_jobs: 0,
    easy_apply_jobs: 0,
    applied_jobs: 0,
    needs_review: 0,
    failed_jobs: 0,
    skipped_jobs: 0,
  };

  const cards: Kpi[] = [
    { label: "Resume", icon: FileText, tone: resume ? "good" : "muted", value: resume ? "Ready" : "Not loaded", note: resume?.filename },
    { label: "LinkedIn", icon: Linkedin, tone: linkedinConnected ? "good" : "muted", value: linkedinConnected ? "Connected" : "Offline" },
    { label: "Dice", icon: Dices, tone: diceConnected ? "good" : "muted", value: diceConnected ? "Connected" : "Offline" },
    { label: "Jobs matched", icon: Target, tone: "", value: s.matched_jobs },
    { label: "Easy Apply", icon: Zap, tone: "", value: s.easy_apply_jobs },
    { label: "Applications", icon: Send, tone: "good", value: s.applied_jobs },
    { label: "Needs review", icon: Eye, tone: "", value: s.needs_review },
    { label: "Failed / skipped", icon: TriangleAlert, tone: s.failed_jobs ? "warn" : "muted", value: `${s.failed_jobs} / ${s.skipped_jobs}` },
  ];
  return (
    <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
      {cards.map((kpi) => (
        <KpiCard key={kpi.label} kpi={kpi} />
      ))}
    </div>
  );
}
