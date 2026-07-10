import { Metric, SectionHeading, EmptyState } from "@/components/ui/misc";
import { performance } from "@/fixtures";

const s = (ms: number) => `${(ms / 1000).toFixed(1)}s`;

export function PerformancePage() {
  if (!performance.available) {
    return (
      <div className="space-y-4">
        <SectionHeading title="Performance" />
        <EmptyState message="No performance run has been recorded yet." />
      </div>
    );
  }
  const slowest = performance.slowest_operation;
  return (
    <div className="space-y-4">
      <SectionHeading title="Performance" />
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Metric label="Total runtime" value={s(performance.total_runtime_ms)} />
        <Metric label="Average Apply" value={s(performance.average_apply_time_ms)} />
        <Metric label="Average Screening" value={s(performance.average_screening_time_ms)} />
        <Metric label="Average Page Load" value={s(performance.average_page_load_ms)} />
      </div>
      <div className="grid grid-cols-2 gap-3 md:grid-cols-3">
        <Metric label="Average Employment" value={s(performance.average_employment_history_time_ms)} />
        <Metric label="Cache hits" value={performance.cache_hits} />
        <Metric label="Cache misses" value={performance.cache_misses} />
      </div>
      {slowest && (
        <p className="text-sm text-[#8A8580]">
          Slowest operation: {slowest.stage} ({s(slowest.elapsed_ms ?? 0)})
        </p>
      )}
      {performance.evidence_path && (
        <p className="text-sm text-[#8A8580]">Evidence: {performance.evidence_path}</p>
      )}
    </div>
  );
}
