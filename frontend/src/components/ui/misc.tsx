import { cn } from "@/lib/utils";
import { Badge } from "./badge";
import type { ApplicationStatus } from "@/types";

/** Progress bar cell (mirrors Streamlit's ProgressColumn). */
export function ProgressCell({ value, max = 100, suffix = "%" }: { value: number; max?: number; suffix?: string }) {
  const pct = Math.max(0, Math.min(100, (value / max) * 100));
  return (
    <div className="flex items-center gap-2">
      <div className="h-1.5 w-16 overflow-hidden rounded-full bg-[#F1EAE0]">
        <div className="h-full rounded-full bg-gradient-to-r from-gold to-gold-deep" style={{ width: `${pct}%` }} />
      </div>
      <span className="tabular-nums text-xs text-[#57534E]">
        {value.toFixed(1)}
        {suffix}
      </span>
    </div>
  );
}

const STATUS_META: Record<ApplicationStatus, { label: string; variant: "success" | "warn" | "danger" | "gold" | "muted" | "default" }> = {
  matched: { label: "Matched", variant: "default" },
  queued: { label: "Queued", variant: "muted" },
  applied: { label: "Applied", variant: "success" },
  needs_review: { label: "Needs Review", variant: "warn" },
  skipped: { label: "Skipped", variant: "muted" },
  failed: { label: "Failed", variant: "danger" },
  dry_run_ready: { label: "Dry Run Ready", variant: "gold" },
  unsupported_form: { label: "Unsupported Form", variant: "danger" },
};

export function StatusBadge({ status }: { status: ApplicationStatus }) {
  const meta = STATUS_META[status] ?? { label: status, variant: "default" as const };
  return <Badge variant={meta.variant} dot>{meta.label}</Badge>;
}

/** Section heading with the brand gold underline accent (mirrors [data-testid=stMain] h2). */
export function SectionHeading({ title, subtitle, className }: { title: string; subtitle?: string; className?: string }) {
  return (
    <div className={cn("mb-3", className)}>
      <h2 className="relative inline-block pb-2 text-xl font-extrabold tracking-tight text-ink after:absolute after:bottom-0 after:left-0 after:h-[3px] after:w-9 after:rounded-sm after:bg-gradient-to-r after:from-gold after:to-gold-deep after:content-['']">
        {title}
      </h2>
      {subtitle && <p className="mt-1.5 text-sm text-[#7A7268]">{subtitle}</p>}
    </div>
  );
}

export function EmptyState({ message }: { message: string }) {
  return (
    <div className="rounded-lg border border-dashed border-parchment-border bg-white/60 px-5 py-8 text-center text-sm text-[#7A7268]">
      {message}
    </div>
  );
}

export function Metric({ label, value, hint }: { label: string; value: React.ReactNode; hint?: string }) {
  return (
    <div className="rounded-lg border border-parchment-border bg-card px-4 py-3 shadow-card transition-transform duration-200 hover:-translate-y-0.5 hover:shadow-lift">
      <div className="text-[.72rem] font-semibold uppercase tracking-[.06em] text-[#8A8580]">{label}</div>
      <div className="mt-1 text-2xl font-extrabold tabular-nums text-ink">{value}</div>
      {hint && <div className="mt-0.5 text-xs text-[#8A8580]">{hint}</div>}
    </div>
  );
}
