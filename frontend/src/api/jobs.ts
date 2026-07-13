import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiRequest } from "@/lib/api";
import { pollWhileActive } from "@/lib/polling";
import type { DashboardSummary, Job, Portal, SearchBatch } from "@/types";

// Phase 4.2: job-search module wired to the real backend. Endpoints (unchanged):
//   GET  /jobs/search-batches?resume_id=&portal=
//   GET  /jobs/search-batches/{batch_id}      (polled while queued/running)
//   GET  /jobs?resume_id=&search_batch_id=&portal=
//   GET  /dashboard/summary?resume_id=&search_batch_id=&portal=
//   POST /jobs/search  { resume_id, location, keywords, limit, portal }

type QueryParams = Record<string, string | number | null | undefined>;

function qs(params: QueryParams): string {
  const usp = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== null && value !== undefined) usp.set(key, String(value));
  }
  const s = usp.toString();
  return s ? `?${s}` : "";
}

export function useSearchBatches(resumeId?: number, portal?: Portal) {
  return useQuery({
    queryKey: ["search-batches", resumeId ?? null, portal ?? null],
    queryFn: () =>
      apiRequest<SearchBatch[]>("GET", `/jobs/search-batches${qs({ resume_id: resumeId, portal })}`),
    enabled: resumeId != null && portal != null,
  });
}

/** Poll a single batch every 2s while queued/running; stop on completed/failed. */
export function useSearchBatch(batchId: string | null) {
  return useQuery({
    queryKey: ["search-batch", batchId],
    queryFn: () => apiRequest<SearchBatch>("GET", `/jobs/search-batches/${batchId}`),
    enabled: batchId != null,
    refetchInterval: (query) => pollWhileActive(query.state.data?.status),
  });
}

export function useJobs(resumeId?: number, searchBatchId?: string | null, portal?: Portal) {
  return useQuery({
    queryKey: ["jobs", resumeId ?? null, searchBatchId ?? null, portal ?? null],
    queryFn: () =>
      apiRequest<Job[]>("GET", `/jobs${qs({ resume_id: resumeId, search_batch_id: searchBatchId, portal })}`),
    enabled: resumeId != null && portal != null,
  });
}

export function useDashboardSummary(resumeId?: number, searchBatchId?: string | null, portal?: Portal) {
  return useQuery({
    queryKey: ["summary", resumeId ?? null, searchBatchId ?? null, portal ?? null],
    queryFn: () =>
      apiRequest<DashboardSummary>(
        "GET",
        `/dashboard/summary${qs({ resume_id: resumeId, search_batch_id: searchBatchId, portal })}`,
      ),
    enabled: portal != null,
  });
}

export interface StartSearchInput {
  resume_id: number;
  location: string;
  keywords: string | null;
  limit: number;
  portal: Portal;
}

export function useStartSearch() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: StartSearchInput) => apiRequest<SearchBatch>("POST", "/jobs/search", { body: input }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["search-batches"] }),
  });
}
