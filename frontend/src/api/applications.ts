import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiRequest } from "@/lib/api";
import { pollWhileActive } from "@/lib/polling";
import type { AutomationState, ApplicationHistory, ApplyTopBatch, Portal } from "@/types";

// GET /applications — scoped by resume_id / search_batch_id / portal exactly as
// dashboard.py does (resume_id omitted when there is no resume).
function qsApplications(resumeId?: number, searchBatchId?: string | null, portal?: Portal): string {
  const usp = new URLSearchParams();
  if (resumeId != null) usp.set("resume_id", String(resumeId));
  if (searchBatchId != null) usp.set("search_batch_id", searchBatchId);
  if (portal != null) usp.set("portal", portal);
  const s = usp.toString();
  return s ? `?${s}` : "";
}

export function useApplications(resumeId?: number, searchBatchId?: string | null, portal?: Portal) {
  return useQuery({
    queryKey: ["applications", resumeId ?? null, searchBatchId ?? null, portal ?? null],
    queryFn: () =>
      apiRequest<ApplicationHistory[]>("GET", `/applications${qsApplications(resumeId, searchBatchId, portal)}`),
    enabled: portal != null,
  });
}

// Phase 4.3: Apply workflow wired to the EXISTING backend endpoints (same as Streamlit).
// React only invokes and renders — no application/automation logic lives here.
//   POST /applications/apply-selected          { resume_id, job_id, dry_run, screening_mode }
//   POST /applications/apply-top/start          { resume_id, limit, dry_run, search_batch_id, screening_mode, portal }
//   GET  /applications/apply-top/batches/{id}   (polled every 2s while queued/running)
//   GET  /applications/apply-top/latest?resume_id=&portal=

export interface ApplySelectedInput {
  resume_id: number;
  job_id: number;
  dry_run: boolean;
  screening_mode: string;
}

// Subset of ApplicationRecordOut needed by the "Selected application status" panel.
export interface ApplyRecordResponse {
  status: string;
  current_step: string | null;
  error_message: string | null;
  evidence_path: string | null;
  evidence_sources: Record<string, unknown>[];
  run_log: Record<string, unknown>[];
}

export function useApplySelected() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: ApplySelectedInput) =>
      apiRequest<ApplyRecordResponse>("POST", "/applications/apply-selected", { body: input }),
    onSuccess: () => {
      // Mirror Streamlit's clear_cached_reads(): jobs/summary/applications may change.
      queryClient.invalidateQueries({ queryKey: ["jobs"] });
      queryClient.invalidateQueries({ queryKey: ["summary"] });
      queryClient.invalidateQueries({ queryKey: ["applications"] });
    },
  });
}

export interface StartApplyTopInput {
  resume_id: number;
  limit: number;
  dry_run: boolean;
  search_batch_id: string | null;
  screening_mode: string;
  portal: Portal;
}

export function useStartApplyTop() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: StartApplyTopInput) =>
      apiRequest<ApplyTopBatch>("POST", "/applications/apply-top/start", { body: input }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["summary"] }),
  });
}

/** Poll an apply-top batch every 2s while queued/running; stop on completed/failed. */
export function useApplyTopBatch(batchId: string | null) {
  return useQuery({
    queryKey: ["apply-top-batch", batchId],
    queryFn: () => apiRequest<ApplyTopBatch>("GET", `/applications/apply-top/batches/${batchId}`),
    enabled: batchId != null,
    refetchInterval: (query) => pollWhileActive(query.state.data?.status),
  });
}

/** Ask the backend to stop the active apply batch.
 *
 *  Returns as soon as the flag is raised -- the worker stops at its next safe checkpoint. The
 *  button is driven by the batch/automation state that follows, never by optimistic local state. */
export function useStopApplyTop() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (batchId: string) =>
      apiRequest<ApplyTopBatch>("POST", `/applications/apply-top/${batchId}/stop`),
    onSuccess: (batch) => {
      queryClient.setQueryData(["apply-top-batch", batch.id], batch);
      queryClient.invalidateQueries({ queryKey: ["automation-state"] });
    },
  });
}

/** Continue an interrupted batch. Resumes the EXISTING batch id -- never starts a new one. */
export function useResumeApplyTop() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (batchId: string) =>
      apiRequest<ApplyTopBatch>("POST", `/applications/apply-top/${batchId}/resume`),
    onSuccess: (batch) => {
      queryClient.setQueryData(["apply-top-batch", batch.id], batch);
      queryClient.invalidateQueries({ queryKey: ["apply-top-latest"] });
      queryClient.invalidateQueries({ queryKey: ["automation-state"] });
    },
  });
}

/** Backend automation state, polled while something is active. */
export function useAutomationState() {
  return useQuery({
    queryKey: ["automation-state"],
    queryFn: () => apiRequest<AutomationState>("GET", "/applications/automation/state"),
    refetchInterval: (query) => (query.state.data?.state === "idle" ? false : 2000),
  });
}

/** Auto-adopt the most recent apply-top batch for this resume/portal (Streamlit parity). */
export function useLatestApplyTopBatch(resumeId?: number, portal?: Portal) {
  return useQuery({
    queryKey: ["apply-top-latest", resumeId ?? null, portal ?? null],
    queryFn: () =>
      apiRequest<ApplyTopBatch | null>(
        "GET",
        `/applications/apply-top/latest?resume_id=${resumeId}&portal=${portal}`,
      ),
    enabled: resumeId != null && portal != null,
  });
}
