import { useEffect, useMemo, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Download, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Field, Select } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Toggle } from "@/components/ui/toggle";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { Alert } from "@/components/ui/alert";
import { SectionHeading, StatusBadge, ProgressCell, Metric, EmptyState } from "@/components/ui/misc";
import { Skeleton } from "@/components/ui/skeleton";
import { apiErrorMessage } from "@/lib/apiError";
import { downloadCsv } from "@/lib/csv";
import { extractScreeningDecisions } from "@/lib/screening";
import { useLatestResume } from "@/api/resume";
import { usePortalStatus } from "@/api/portal";
import { useJobs, useStartSearch } from "@/api/jobs";
import { useScreeningSettings } from "@/api/screening";
import {
  useApplySelected,
  useStartApplyTop,
  useApplyTopBatch,
  useLatestApplyTopBatch,
} from "@/api/applications";
import { useSession } from "@/store/session";
import type { ApplyResultView, ApplyTopBatch, Job } from "@/types";

const LOCATION_OPTIONS = [
  "United States", "Remote", "New York, NY", "Austin, TX", "San Francisco, CA",
  "Seattle, WA", "Chicago, IL", "Boston, MA", "Los Angeles, CA", "Dallas, TX", "Custom",
];

function jobRadioLabel(job: Job): string {
  const portal = job.portal.charAt(0).toUpperCase() + job.portal.slice(1);
  return `${portal} | ${job.title} | ${job.company} | ${job.match_score.toFixed(1)}%` + (job.easy_apply ? " | Easy Apply" : "");
}

const titleize = (s: string) => s.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
const dictCell = (v: unknown) => (v === null || v === undefined ? "—" : String(v));

/** Live "Apply Top 10 batch status" card (mirrors dashboard.py apply_top_status_card). */
function ApplyTopStatusCard({
  batch,
  onRefresh,
  onHide,
}: {
  batch: ApplyTopBatch;
  onRefresh: () => void;
  onHide: () => void;
}) {
  const statusTitle = titleize(batch.status ?? "unknown");
  const canHide = batch.status === "completed" || batch.status === "failed";
  const pct = batch.total_count ? Math.min(100, (batch.current_index / batch.total_count) * 100) : 0;
  const results = batch.job_results ?? [];

  return (
    <Card>
      <CardHeader>
        <CardTitle>Apply Top 10 batch status</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <Alert variant="info">{batch.latest_status_message ?? statusTitle}</Alert>
        <div className="h-1.5 w-full overflow-hidden rounded-full bg-[#F1EAE0]">
          <div className="h-full rounded-full bg-gradient-to-r from-gold to-gold-deep transition-[width] duration-500" style={{ width: `${pct}%` }} />
        </div>
        <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
          <Metric label="Status" value={statusTitle} />
          <Metric label="Current job" value={`${batch.current_index}/${batch.total_count}`} />
          <Metric label="Completed" value={batch.completed_count} />
          <Metric label="Needs review" value={batch.needs_review_count} />
          <Metric label="Failed/Timed out" value={batch.failed_count + batch.timed_out_count} />
        </div>
        {(batch.current_title || batch.current_company) && (
          <p className="text-sm text-[#8A8580]">
            Now processing: {batch.current_title ?? "Unknown"} at {batch.current_company ?? "Unknown"}
          </p>
        )}
        {batch.error_message && <Alert variant="danger">{batch.error_message}</Alert>}
        {batch.debug_path && <p className="text-xs text-[#8A8580]">Debug log: {batch.debug_path}</p>}
        {results.length > 0 && (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>#</TableHead>
                <TableHead>Title</TableHead>
                <TableHead>Company</TableHead>
                <TableHead>Status</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {results.map((r, i) => (
                <TableRow key={i}>
                  <TableCell className="tabular-nums">{dictCell(r.index)}</TableCell>
                  <TableCell className="font-medium text-ink">{dictCell(r.title)}</TableCell>
                  <TableCell>{dictCell(r.company)}</TableCell>
                  <TableCell>{titleize(String(r.status ?? "—"))}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
        <div className="flex gap-2">
          <Button onClick={onRefresh}>Refresh Batch Status</Button>
          {canHide && (
            <Button variant="ghost" onClick={onHide}>
              Hide Batch Status
            </Button>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

/** Live "Selected application status" panel (mirrors dashboard.py). */
function SelectedApplicationPanel({ result, pending }: { result: ApplyResultView | null; pending: boolean }) {
  if (!result && !pending) return null;
  const decisions = result ? extractScreeningDecisions(result) : [];
  const needsReview = decisions.filter((d) => d.needs_review);

  return (
    <Card>
      <CardHeader>
        <CardTitle>Selected application status</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {pending && (
          <Alert variant="info">
            <Loader2 className="mr-1 inline size-4 animate-spin" /> Processing selected Easy Apply job…
          </Alert>
        )}
        {result && (
          <>
            <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
              <Metric label="Selected job" value={result.job_title} />
              <Metric label="Status" value={titleize(result.status ?? "unknown")} />
              <Metric label="Last step" value={titleize(result.current_step ?? "Not started")} />
            </div>
            {result.error_message ? (
              result.status === "failed" ? (
                <Alert variant="danger">{result.error_message}</Alert>
              ) : (
                <Alert variant="warn">{result.error_message}</Alert>
              )
            ) : (
              <Alert variant="success">Selected job workflow completed successfully.</Alert>
            )}
            {result.evidence_path && <p className="text-xs text-[#8A8580]">Evidence saved to: {result.evidence_path}</p>}
            {decisions.length > 0 && (
              <div>
                <div className="mb-1 text-sm font-bold text-ink">Screening answers (dry run)</div>
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Question</TableHead>
                      <TableHead>Answer</TableHead>
                      <TableHead>Source</TableHead>
                      <TableHead>Confidence</TableHead>
                      <TableHead>Reasoning</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {decisions.map((d, i) => (
                      <TableRow key={i}>
                        <TableCell className="font-medium text-ink">{d.question}</TableCell>
                        <TableCell>{d.answer ?? <Badge variant="warn">needs review</Badge>}</TableCell>
                        <TableCell>{d.source ?? "—"}</TableCell>
                        <TableCell>{d.confidence ?? "—"}</TableCell>
                        <TableCell className="max-w-sm text-xs">{d.reasoning}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
                {needsReview.length > 0 && (
                  <Alert variant="warn" className="mt-2">
                    {needsReview.length} question(s) need review (no confident, evidence-backed answer):{" "}
                    {needsReview.slice(0, 5).map((d) => d.question).join(", ")}
                  </Alert>
                )}
                <p className="mt-1 text-xs text-[#8A8580]">
                  Full JSON logs are under outputs/screening_debug/ and outputs/dice_debug/.
                </p>
              </div>
            )}
          </>
        )}
      </CardContent>
    </Card>
  );
}

export function MatchedJobsPage() {
  const { data: resume } = useLatestResume();
  const { data: portalStatus } = usePortalStatus();
  const { data: screeningSettings } = useScreeningSettings();
  const { selectedBatchId, setSelectedBatchId } = useSession();
  const queryClient = useQueryClient();

  const portal = portalStatus?.active_portal;
  // Match dashboard.py's `(screening_settings.get("mode") or "safe").lower()` — Python
  // `or` falls back on an empty string too, so use || (not ??) for exact parity.
  const screeningMode = (screeningSettings?.mode || "safe").toLowerCase();
  const jobsQuery = useJobs(resume?.id, selectedBatchId, portal);
  const startSearch = useStartSearch();
  const jobs = jobsQuery.data ?? [];

  const suggestedRoles = (resume?.role_suggestions ?? []).map((r) => r.role);
  const primaryRole = suggestedRoles[0] ?? "Software Engineer";
  const searchOptions = [
    "All suggested roles",
    `Primary role: ${primaryRole}`,
    ...suggestedRoles.slice(1).map((r) => `Suggested role: ${r}`),
    "Manual override",
  ];

  const [searchChoice, setSearchChoice] = useState(searchOptions[0]);
  const [locationChoice, setLocationChoice] = useState(LOCATION_OPTIONS[0]);
  const [manualRole, setManualRole] = useState("");
  const [customLocation, setCustomLocation] = useState("");
  const [customLimit, setCustomLimit] = useState(100);
  const [selectedLabel, setSelectedLabel] = useState<string | null>(null);
  const [dryRun, setDryRun] = useState(true); // default ON — feeds the real dry_run flag

  // Apply state
  const applySelected = useApplySelected();
  const startApplyTop = useStartApplyTop();
  const [applyTopBatchId, setApplyTopBatchId] = useState<string | null>(null);
  const [hideBatch, setHideBatch] = useState(false);
  const [selectedResult, setSelectedResult] = useState<ApplyResultView | null>(null);

  const latestBatch = useLatestApplyTopBatch(resume?.id, portal);
  const effectiveBatchId = applyTopBatchId ?? (hideBatch ? null : latestBatch.data?.id ?? null);
  const batchQuery = useApplyTopBatch(effectiveBatchId);
  const liveBatch = batchQuery.data ?? null;

  // When an apply-top batch reaches a terminal state, refresh jobs/summary/applications once.
  const prevBatchStatus = useRef<string | null>(null);
  useEffect(() => {
    const was = prevBatchStatus.current;
    const now = liveBatch?.status ?? null;
    if ((was === "running" || was === "queued") && (now === "completed" || now === "failed")) {
      queryClient.invalidateQueries({ queryKey: ["jobs"] });
      queryClient.invalidateQueries({ queryKey: ["summary"] });
      queryClient.invalidateQueries({ queryKey: ["applications"] });
    }
    prevBatchStatus.current = now;
  }, [liveBatch?.status, queryClient]);

  const selectedJob = jobs.find((j) => jobRadioLabel(j) === selectedLabel) ?? null;
  const searchDisabled = resume == null || startSearch.isPending;
  const applyBulkDisabled = resume == null || startApplyTop.isPending;

  const resolveKeywords = (): string | null => {
    // Mirror dashboard.py exactly: "All suggested roles" -> None; "Manual override" ->
    // the raw text_input value (may be ""); otherwise the substring after the FIRST
    // ": " (Python's split(": ", 1)[1]) so role names containing ": " are preserved.
    if (searchChoice === "All suggested roles") return null;
    if (searchChoice === "Manual override") return manualRole;
    const idx = searchChoice.indexOf(": ");
    return idx === -1 ? searchChoice : searchChoice.slice(idx + 2);
  };
  const resolveLocation = () => (locationChoice === "Custom" ? customLocation : locationChoice);

  const runSearch = (limit: number) => {
    if (!resume || !portal) return;
    startSearch.mutate(
      { resume_id: resume.id, location: resolveLocation(), keywords: resolveKeywords(), limit, portal },
      { onSuccess: (batch) => setSelectedBatchId(batch.id) },
    );
  };

  const applySelectedJob = () => {
    if (!resume || !selectedJob) return;
    applySelected.mutate(
      { resume_id: resume.id, job_id: selectedJob.id, dry_run: dryRun, screening_mode: screeningMode },
      {
        onSuccess: (record) =>
          setSelectedResult({ ...record, job_title: selectedJob.title, company: selectedJob.company }),
        onError: (err) =>
          setSelectedResult({
            job_title: selectedJob.title,
            company: selectedJob.company,
            status: "failed",
            current_step: "request_failed",
            error_message: apiErrorMessage("Apply Selected Job", err),
            evidence_path: null,
          }),
      },
    );
  };

  const runApplyTop = (limit: number) => {
    if (!resume || !portal) return;
    startApplyTop.mutate(
      { resume_id: resume.id, limit, dry_run: dryRun, search_batch_id: selectedBatchId, screening_mode: screeningMode, portal },
      { onSuccess: (batch) => { setApplyTopBatchId(batch.id); setHideBatch(false); } },
    );
  };

  const exportCsv = () =>
    downloadCsv("matched_jobs.csv", jobs, [
      { key: "title", label: "Title" },
      { key: "company", label: "Company" },
      { key: "portal", label: "Portal" },
      { key: "location", label: "Location" },
      { key: "search_role", label: "Search Role" },
      { key: "role_priority", label: "Role Priority" },
      { key: "match_score", label: "Match" },
      { key: "application_friction_score", label: "Friction" },
      { key: "recommended_apply_priority", label: "Apply Priority" },
      { key: "job_url", label: "Job URL" },
    ]);

  // The matched-jobs table and single-job radio list can each hold up to ~250 rows.
  // Memoise them so the heavy row work only reruns when the jobs (or the current radio
  // selection) actually change — not on every Dry Run toggle or search-control keystroke.
  const jobRows = useMemo(
    () =>
      jobs.map((job) => (
        <TableRow key={job.id}>
          <TableCell className="font-semibold text-ink">{job.title}</TableCell>
          <TableCell>{job.company}</TableCell>
          <TableCell className="capitalize">{job.portal}</TableCell>
          <TableCell><ProgressCell value={job.match_score} /></TableCell>
          <TableCell><ProgressCell value={job.application_friction_score} suffix="" /></TableCell>
          <TableCell>
            <Badge variant={job.recommended_apply_priority === "High" ? "success" : job.recommended_apply_priority === "Low" ? "muted" : "gold"}>
              {job.recommended_apply_priority ?? "—"}
            </Badge>
          </TableCell>
          <TableCell>{job.easy_apply ? "Yes" : "No"}</TableCell>
          <TableCell><StatusBadge status={job.status} /></TableCell>
          <TableCell>
            <a href={job.job_url} target="_blank" rel="noreferrer" className="font-semibold text-gold-text hover:underline">
              Open
            </a>
          </TableCell>
        </TableRow>
      )),
    [jobs],
  );

  const jobRadios = useMemo(
    () =>
      jobs.map((job) => {
        const label = jobRadioLabel(job);
        return (
          <label key={job.id} className="flex cursor-pointer items-center gap-2 text-sm text-[#4B5563]">
            <input
              type="radio"
              name="job-select"
              className="accent-[#D97706]"
              checked={selectedLabel === label}
              onChange={() => setSelectedLabel(label)}
            />
            {label}
          </label>
        );
      }),
    [jobs, selectedLabel],
  );

  return (
    <div className="space-y-5">
      <SectionHeading title="Matched jobs" subtitle="Search, rank and dry-run apply to jobs for the active portal." />

      {/* Search controls — LIVE: POST /jobs/search */}
      <div className="grid grid-cols-1 gap-3 md:grid-cols-[2fr_2fr_1fr]">
        <Field label="Search using" hint="Search all detected roles, one suggested role, or enter a manual override.">
          <Select options={searchOptions} value={searchChoice} onChange={(e) => setSearchChoice(e.target.value)} />
          {searchChoice === "Manual override" && (
            <Field label="Manual role">
              <Input placeholder="Enter a job title" value={manualRole} onChange={(e) => setManualRole(e.target.value)} />
            </Field>
          )}
        </Field>
        <Field label="Location">
          <Select options={LOCATION_OPTIONS} value={locationChoice} onChange={(e) => setLocationChoice(e.target.value)} />
          {locationChoice === "Custom" && (
            <Field label="Custom location">
              <Input placeholder="City, State or country" value={customLocation} onChange={(e) => setCustomLocation(e.target.value)} />
            </Field>
          )}
        </Field>
        <div className="flex flex-col gap-2">
          <Field label="Custom limit">
            <Input
              type="number"
              min={1}
              max={100}
              step={5}
              value={customLimit}
              onChange={(e) => setCustomLimit(Number(e.target.value))}
              disabled={searchDisabled}
            />
          </Field>
          <div className="grid grid-cols-2 gap-2">
            <Button disabled={searchDisabled} onClick={() => runSearch(20)}>Find 20</Button>
            <Button variant="primary" disabled={searchDisabled} onClick={() => runSearch(50)}>Find 50</Button>
            <Button disabled={searchDisabled} onClick={() => runSearch(100)}>Find 100</Button>
            <Button disabled={searchDisabled} onClick={() => runSearch(Math.min(100, customLimit))}>Find custom</Button>
          </div>
        </div>
      </div>
      {startSearch.isPending && (
        <Alert variant="info">
          <Loader2 className="mr-1 inline size-4 animate-spin" /> Starting background search…
        </Alert>
      )}
      {startSearch.isError && <Alert variant="danger">{apiErrorMessage("Start search batch", startSearch.error)}</Alert>}

      {/* Matched jobs table — LIVE: GET /jobs */}
      {jobsQuery.isLoading ? (
        <Skeleton className="h-40" />
      ) : jobsQuery.isError ? (
        <Alert variant="danger">{apiErrorMessage("Load jobs", jobsQuery.error)}</Alert>
      ) : jobs.length === 0 ? (
        <EmptyState message="No matched jobs yet." />
      ) : (
        <>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Title</TableHead>
                <TableHead>Company</TableHead>
                <TableHead>Portal</TableHead>
                <TableHead>Match</TableHead>
                <TableHead>Friction</TableHead>
                <TableHead>Apply Priority</TableHead>
                <TableHead>Easy Apply</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Job URL</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>{jobRows}</TableBody>
          </Table>

          <Button onClick={exportCsv}>
            <Download className="size-4" /> Export matched jobs CSV
          </Button>

          {/* Single-job selector */}
          <Card>
            <CardHeader>
              <CardTitle>Select one job for individual testing</CardTitle>
            </CardHeader>
            <CardContent className="space-y-2">
              {jobRadios}
              {selectedJob && (
                <p className="pt-1 text-sm text-[#8A8580]">
                  Selected job: {selectedJob.title} at {selectedJob.company}
                </p>
              )}
            </CardContent>
          </Card>

          {/* Apply action bar — LIVE: apply-selected / apply-top/start */}
          <div className="flex flex-wrap items-center gap-3 rounded-lg border border-parchment-border bg-card p-3 shadow-card">
            <Toggle id="dry-run" checked={dryRun} onCheckedChange={setDryRun} label="Dry run" />
            <div className="flex flex-1 flex-wrap justify-end gap-2">
              <Button variant="primary" disabled={selectedJob === null || applySelected.isPending} onClick={applySelectedJob}>
                Apply Selected Job
              </Button>
              <Button disabled={applyBulkDisabled} onClick={() => runApplyTop(10)}>Apply Top 10</Button>
              <Button disabled={applyBulkDisabled} onClick={() => runApplyTop(20)}>Apply Top 20</Button>
              <Button disabled={applyBulkDisabled} onClick={() => runApplyTop(Math.min(jobs.length, 50))}>Apply All</Button>
            </div>
          </div>
          {jobs.length > 50 && (
            <p className="text-sm text-[#8A8580]">Apply All is safety-capped at 50 ranked jobs per batch.</p>
          )}
          {startApplyTop.isPending && (
            <Alert variant="info">
              <Loader2 className="mr-1 inline size-4 animate-spin" /> Starting Apply batch…
            </Alert>
          )}
          {startApplyTop.isError && <Alert variant="danger">{apiErrorMessage("Start Apply batch", startApplyTop.error)}</Alert>}

          {/* Apply Top batch status card — LIVE with 2s polling */}
          {liveBatch && (
            <ApplyTopStatusCard
              batch={liveBatch}
              onRefresh={() => {
                batchQuery.refetch();
                queryClient.invalidateQueries({ queryKey: ["jobs"] });
                queryClient.invalidateQueries({ queryKey: ["summary"] });
              }}
              onHide={() => { setApplyTopBatchId(null); setHideBatch(true); }}
            />
          )}

          {/* Selected application status panel — LIVE */}
          <SelectedApplicationPanel result={selectedResult} pending={applySelected.isPending} />
        </>
      )}
    </div>
  );
}
