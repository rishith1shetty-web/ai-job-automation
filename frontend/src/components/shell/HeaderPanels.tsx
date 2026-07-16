import { useEffect, useRef } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Metric, ProgressCell, EmptyState } from "@/components/ui/misc";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Field, Select } from "@/components/ui/field";
import { Alert } from "@/components/ui/alert";
import { useLatestResume } from "@/api/resume";
import { usePortalStatus } from "@/api/portal";
import { useSearchBatch, useSearchBatches } from "@/api/jobs";
import { useSession } from "@/store/session";
import { apiErrorMessage } from "@/lib/apiError";
import type { SearchBatch } from "@/types";

function StatSkeleton() {
  return (
    <div className="grid grid-cols-3 gap-3">
      {[0, 1, 2].map((i) => (
        <Skeleton key={i} className="h-[70px]" />
      ))}
    </div>
  );
}

/** 3 stat cards + active-resume caption — LIVE: GET /resumes/latest. */
export function ResumeSummaryStrip() {
  const { data: resume, isLoading, isError, error } = useLatestResume();

  if (isLoading) return <StatSkeleton />;
  if (isError) return <Alert variant="danger">{apiErrorMessage("Load resume", error)}</Alert>;
  if (!resume) {
    return <EmptyState message="No resume analyzed yet. Upload a resume in the sidebar to begin." />;
  }

  return (
    <div className="space-y-2">
      <div className="grid grid-cols-3 gap-3">
        <Metric label="Skills" value={resume.skills.length} />
        <Metric label="Job titles" value={resume.job_titles.length} />
        <Metric label="Technologies" value={resume.technologies.length} />
      </div>
      <p className="text-sm text-[#8A8580]">Active resume: {resume.filename}</p>
    </div>
  );
}

const CATEGORY_LABELS: Record<string, string> = {
  primary_role: "Primary Role",
  strong_fit_roles: "Strong Fit Roles",
  related_roles: "Related Roles",
  stretch_roles: "Stretch Roles",
};

/** Detected candidate profile: primary role, suggestions, confidence — LIVE resume. */
export function DetectedProfilePanel() {
  const { data: resume } = useLatestResume();
  if (!resume) return null;

  const suggestions =
    resume.role_suggestions.length > 0
      ? resume.role_suggestions
      : [{ role: resume.detected_role ?? "Software Engineer", confidence: resume.role_confidence }];
  const primary = suggestions[0];
  const groups = ["primary_role", "strong_fit_roles", "related_roles", "stretch_roles"] as const;

  return (
    <section className="space-y-3">
      <h2 className="text-lg font-extrabold tracking-tight text-ink">Detected candidate profile</h2>
      <div className="grid grid-cols-1 gap-3 md:grid-cols-[2fr_1fr]">
        <Metric label="Primary role" value={primary.role} />
        <Metric label="Confidence" value={`${primary.confidence.toFixed(0)}%`} />
      </div>

      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Rank</TableHead>
            <TableHead>Suggested role</TableHead>
            <TableHead>Category</TableHead>
            <TableHead>Confidence</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {suggestions.map((sug, i) => (
            <TableRow key={`${sug.role}-${i}`}>
              <TableCell className="tabular-nums">{i + 1}</TableCell>
              <TableCell className="font-semibold text-ink">{sug.role}</TableCell>
              <TableCell>{CATEGORY_LABELS[sug.role_category ?? ""] ?? "—"}</TableCell>
              <TableCell>
                <ProgressCell value={sug.confidence} />
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        {groups.map((g) => {
          const values = suggestions.filter((sug) => sug.role_category === g).map((sug) => sug.role);
          return (
            <div key={g}>
              <div className="text-xs font-semibold uppercase tracking-wide text-[#8A8580]">
                {CATEGORY_LABELS[g]}
              </div>
              <div className="mt-1 text-sm text-[#57534E]">{values.length ? values.join(", ") : "None"}</div>
            </div>
          );
        })}
      </div>

      <div>
        <div className="mb-1 text-sm font-bold text-ink">Generated LinkedIn search query</div>
        <pre className="overflow-x-auto rounded-md border border-parchment-border bg-[#FBF7F0] px-3 py-2 text-xs text-brown-deep">
          {resume.generated_search_query ?? (resume.generated_search_keywords ?? []).join(" OR ")}
        </pre>
        {resume.domain_expertise.length > 0 && (
          <p className="mt-1 text-xs text-[#8A8580]">Domain expertise: {resume.domain_expertise.join(", ")}</p>
        )}
      </div>
    </section>
  );
}

// Placeholder shown when no batch is selected — the state a fresh session starts in.
const NO_BATCH_OPTION = "Select a search batch…";

function batchOptionLabel(b: SearchBatch): string {
  const portal = b.portal.charAt(0).toUpperCase() + b.portal.slice(1);
  return `${b.created_at.slice(0, 19).replace("T", " ")} | ${portal} | ${b.location} | ${b.job_count} jobs`;
}

/** Live "Find Matches batch status" card (mirrors dashboard.py search_batch_status_card). */
function SearchBatchStatusCard({ batch }: { batch: SearchBatch }) {
  const status = (batch.status ?? "unknown").replace(/_/g, " ");
  const statusTitle = status.charAt(0).toUpperCase() + status.slice(1);
  return (
    <div className="space-y-2 rounded-lg border border-parchment-border bg-card p-3 shadow-card">
      <h3 className="text-base font-bold text-ink">Find Matches batch status</h3>
      {batch.status === "failed" ? (
        <Alert variant="danger">{batch.error_message ?? "Search failed in backend."}</Alert>
      ) : (
        <Alert variant="info">{batch.latest_status_message ?? statusTitle}</Alert>
      )}
      <div className="grid grid-cols-2 gap-2 md:grid-cols-5">
        <Metric label="Status" value={statusTitle} />
        <Metric label="Jobs collected" value={batch.jobs_collected || batch.job_count || 0} />
        <Metric label="Easy Apply" value={batch.easy_apply_count || 0} />
        <Metric label="Duplicates removed" value={batch.duplicates_removed || 0} />
        <Metric label="Requested" value={batch.requested_limit || 0} />
      </div>
      {batch.current_search_variation && (
        <p className="text-xs text-[#8A8580]">Current search variation: {batch.current_search_variation}</p>
      )}
    </div>
  );
}

/** Search batch selector + live status card — LIVE with 2s polling while running. */
export function SearchBatchBar() {
  const { data: resume } = useLatestResume();
  const { data: status } = usePortalStatus();
  const { selectedBatchId, setSelectedBatchId } = useSession();
  const { data: batches } = useSearchBatches(resume?.id, status?.active_portal);
  const polled = useSearchBatch(selectedBatchId);
  const queryClient = useQueryClient();
  const prevStatus = useRef<string | null>(null);

  // A previous batch is NEVER auto-selected: a fresh session starts with nothing selected
  // so old Find Matches results cannot reappear on the dashboard. Historical batches stay
  // available in the dropdown and are shown only when the user explicitly picks one; a new
  // search selects its own batch (MatchedJobsPage). Only drop a selection that no longer
  // exists in the list (e.g. after switching resume/portal).
  useEffect(() => {
    if (selectedBatchId == null || !batches) return;
    if (!batches.some((b) => b.id === selectedBatchId)) {
      setSelectedBatchId(null);
    }
  }, [batches, selectedBatchId, setSelectedBatchId]);

  // When a running batch reaches a terminal state, refresh jobs + summary once.
  const liveStatus = polled.data?.status ?? null;
  useEffect(() => {
    const was = prevStatus.current;
    if ((was === "running" || was === "queued") && (liveStatus === "completed" || liveStatus === "failed")) {
      queryClient.invalidateQueries({ queryKey: ["jobs"] });
      queryClient.invalidateQueries({ queryKey: ["summary"] });
      queryClient.invalidateQueries({ queryKey: ["search-batches"] });
    }
    prevStatus.current = liveStatus;
  }, [liveStatus, queryClient]);

  if (!resume) return null;
  if (!batches || batches.length === 0) {
    return <p className="text-sm text-[#8A8580]">No saved search batches yet. Existing legacy jobs remain available.</p>;
  }

  const options = batches.map(batchOptionLabel);
  const selectedIndex = batches.findIndex((b) => b.id === selectedBatchId);
  // Nothing selected means nothing shown — no falling back to the most recent batch, which
  // would put a previous session's results back on the dashboard.
  const selectedListEntry = selectedIndex >= 0 ? batches[selectedIndex] : null;
  const effective = selectedListEntry ? polled.data ?? selectedListEntry : null;
  const showStatusCard = effective != null && ["queued", "running", "failed"].includes(effective.status);
  const shortfall =
    effective != null &&
    effective.status === "completed" &&
    effective.jobs_collected < effective.requested_limit;

  return (
    <section className="space-y-2">
      <Field label="Search batch">
        <Select
          options={[NO_BATCH_OPTION, ...options]}
          value={selectedIndex >= 0 ? options[selectedIndex] : NO_BATCH_OPTION}
          onChange={(e) => {
            const picked = e.target.value;
            setSelectedBatchId(
              picked === NO_BATCH_OPTION ? null : batches[options.indexOf(picked)].id,
            );
          }}
        />
      </Field>
      {selectedListEntry && (
        <p className="text-sm text-[#8A8580]">Search query: {selectedListEntry.search_query}</p>
      )}
      {showStatusCard && effective && <SearchBatchStatusCard batch={effective} />}
      {shortfall && effective && (
        <Alert variant="warn">
          Found {effective.jobs_collected} jobs out of requested {effective.requested_limit} based on current
          results.
        </Alert>
      )}
    </section>
  );
}
