import { Metric, SectionHeading, EmptyState } from "@/components/ui/misc";
import { Skeleton } from "@/components/ui/skeleton";
import { usePerformance } from "@/api/evidence";

const secs = (ms: number | undefined) => `${((ms ?? 0) / 1000).toFixed(1)}s`;

export function PerformancePage() {
  const { data, isLoading } = usePerformance();

  if (isLoading) {
    return <Skeleton className="h-40" />;
  }
  if (!data?.available) {
    return (
      <div className="space-y-4">
        <SectionHeading title="Performance" />
        <EmptyState message="No performance run has been recorded yet." />
      </div>
    );
  }

  const slowest = data.slowest_operation;
  return (
    <div className="space-y-4">
      <SectionHeading title="Performance" />
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Metric label="Total runtime" value={secs(data.total_runtime_ms)} />
        <Metric label="Average Apply" value={secs(data.average_apply_time_ms)} />
        <Metric label="Average Screening" value={secs(data.average_screening_time_ms)} />
        <Metric label="Average Page Load" value={secs(data.average_page_load_ms)} />
      </div>
      <div className="grid grid-cols-2 gap-3 md:grid-cols-3">
        <Metric label="Average Employment" value={secs(data.average_employment_history_time_ms)} />
        <Metric label="Cache hits" value={data.cache_hits ?? 0} />
        <Metric label="Cache misses" value={data.cache_misses ?? 0} />
      </div>
      {slowest && (
        <p className="text-sm text-[#8A8580]">
          Slowest operation: {slowest.stage} ({secs(slowest.elapsed_ms)})
        </p>
      )}
      {data.evidence_path && <p className="text-sm text-[#8A8580]">Evidence: {data.evidence_path}</p>}
    </div>
  );
}
