import { useState } from "react";
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
import { mockAction } from "@/lib/mock";
import { apiErrorMessage } from "@/lib/apiError";
import { downloadCsv } from "@/lib/csv";
import { useLatestResume } from "@/api/resume";
import { usePortalStatus } from "@/api/portal";
import { useJobs, useStartSearch } from "@/api/jobs";
import { useSession } from "@/store/session";
import { applyTopBatch, selectedApplicationResult } from "@/fixtures";
import type { Job } from "@/types";

const LOCATION_OPTIONS = [
  "United States", "Remote", "New York, NY", "Austin, TX", "San Francisco, CA",
  "Seattle, WA", "Chicago, IL", "Boston, MA", "Los Angeles, CA", "Dallas, TX", "Custom",
];

function jobRadioLabel(job: Job): string {
  const portal = job.portal.charAt(0).toUpperCase() + job.portal.slice(1);
  return `${portal} | ${job.title} | ${job.company} | ${job.match_score.toFixed(1)}%` + (job.easy_apply ? " | Easy Apply" : "");
}

export function MatchedJobsPage() {
  const { data: resume } = useLatestResume();
  const { data: portalStatus } = usePortalStatus();
  const { selectedBatchId, setSelectedBatchId } = useSession();
  const portal = portalStatus?.active_portal;
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
  const [dryRun, setDryRun] = useState(true); // default ON, matching Streamlit (local UI only)
  const [showBatch, setShowBatch] = useState(true);

  const selectedJob = jobs.find((j) => jobRadioLabel(j) === selectedLabel) ?? null;
  const searchDisabled = resume == null || startSearch.isPending;

  const resolveKeywords = (): string | null => {
    if (searchChoice === "All suggested roles") return null;
    if (searchChoice === "Manual override") return manualRole || null;
    return searchChoice.split(": ", 2)[1] ?? null;
  };
  const resolveLocation = () => (locationChoice === "Custom" ? customLocation : locationChoice);

  const runSearch = (limit: number) => {
    if (!resume || !portal) return;
    startSearch.mutate(
      { resume_id: resume.id, location: resolveLocation(), keywords: resolveKeywords(), limit, portal },
      { onSuccess: (batch) => setSelectedBatchId(batch.id) },
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
        <div className="h-40 animate-pulse rounded-lg border border-parchment-border bg-[#F3EDE3]" />
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
            <TableBody>
              {jobs.map((job) => (
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
              ))}
            </TableBody>
          </Table>

          <Button onClick={exportCsv}>
            <Download className="size-4" /> Export matched jobs CSV
          </Button>

          {/* Single-job selector (feeds the still-inert Apply Selected button) */}
          <Card>
            <CardHeader>
              <CardTitle>Select one job for individual testing</CardTitle>
            </CardHeader>
            <CardContent className="space-y-2">
              {jobs.map((job) => {
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
              })}
              {selectedJob && (
                <p className="pt-1 text-sm text-[#8A8580]">
                  Selected job: {selectedJob.title} at {selectedJob.company}
                </p>
              )}
            </CardContent>
          </Card>

          {/* Apply action bar — STILL MOCKED / INERT (Phase 4.2 does not wire apply) */}
          <div className="flex flex-wrap items-center gap-3 rounded-lg border border-parchment-border bg-card p-3 shadow-card">
            <Toggle id="dry-run" checked={dryRun} onCheckedChange={setDryRun} label="Dry run" />
            <div className="flex flex-1 flex-wrap justify-end gap-2">
              <Button variant="primary" disabled={selectedJob === null} onClick={() => mockAction("Apply Selected Job")}>
                Apply Selected Job
              </Button>
              <Button onClick={() => mockAction("Apply Top 10")}>Apply Top 10</Button>
              <Button onClick={() => mockAction("Apply Top 20")}>Apply Top 20</Button>
              <Button onClick={() => mockAction("Apply All")}>Apply All</Button>
            </div>
          </div>
          {jobs.length > 50 && (
            <p className="text-sm text-[#8A8580]">Apply All is safety-capped at 50 ranked jobs per batch.</p>
          )}

          {/* Apply Top batch status card — STILL MOCKED */}
          {showBatch && (
            <Card>
              <CardHeader>
                <CardTitle>Apply Top 10 batch status</CardTitle>
              </CardHeader>
              <CardContent className="space-y-3">
                <Alert variant="info">{applyTopBatch.latest_status_message}</Alert>
                <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
                  <Metric label="Status" value={applyTopBatch.status.replace("_", " ")} />
                  <Metric label="Current job" value={`${applyTopBatch.current_index}/${applyTopBatch.total_count}`} />
                  <Metric label="Completed" value={applyTopBatch.completed_count} />
                  <Metric label="Needs review" value={applyTopBatch.needs_review_count} />
                  <Metric label="Failed/Timed out" value={applyTopBatch.failed_count + applyTopBatch.timed_out_count} />
                </div>
                <div className="flex gap-2">
                  <Button onClick={() => mockAction("Refresh Batch Status")}>Refresh Batch Status</Button>
                  <Button variant="ghost" onClick={() => { mockAction("Hide Batch Status"); setShowBatch(false); }}>
                    Hide Batch Status
                  </Button>
                </div>
              </CardContent>
            </Card>
          )}

          {/* Selected application result panel — STILL MOCKED */}
          <Card>
            <CardHeader>
              <CardTitle>Selected application status</CardTitle>
            </CardHeader>
            <CardContent className="space-y-3">
              <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
                <Metric label="Selected job" value={selectedApplicationResult.job_title} />
                <Metric label="Status" value={selectedApplicationResult.status.replace(/_/g, " ")} />
                <Metric label="Last step" value={(selectedApplicationResult.current_step ?? "Not started").replace(/_/g, " ")} />
              </div>
              <Alert variant="success">Selected job workflow completed successfully.</Alert>
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
                    {(selectedApplicationResult.evidence_sources ?? []).map((d, i) => (
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
              </div>
            </CardContent>
          </Card>
        </>
      )}
    </div>
  );
}
