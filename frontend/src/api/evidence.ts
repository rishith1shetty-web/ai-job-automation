import { useQuery } from "@tanstack/react-query";
import { apiRequest } from "@/lib/api";

// Phase 4.4: read-only evidence/diagnostics endpoints. These return JSON blobs read
// from outputs/ on the backend, so they are typed permissively (the backend is the
// source of truth; React only renders what it returns — it generates nothing).
export type Dict = Record<string, unknown>;

export interface AvailablePayload extends Dict {
  available?: boolean;
  folder?: string;
}

function evidenceQuery<T extends AvailablePayload>(key: string, path: string) {
  return {
    queryKey: [key],
    queryFn: () => apiRequest<T>("GET", path),
  } as const;
}

export function useResumeIntelligence() {
  return useQuery(evidenceQuery<AvailablePayload>("resume-intelligence-latest", "/resume-intelligence/latest"));
}

export function useReasoning() {
  return useQuery(evidenceQuery<AvailablePayload>("reasoning-latest", "/reasoning/latest"));
}

export function useFormLearning() {
  return useQuery({
    queryKey: ["form-learning"],
    queryFn: () => apiRequest<Dict[]>("GET", "/form-learning"),
  });
}

export function useFormLearningLatest() {
  return useQuery(evidenceQuery<AvailablePayload>("form-learning-latest", "/form-learning/latest"));
}

export function useGeneralizedForm() {
  return useQuery({
    queryKey: ["generalized-form-engine"],
    queryFn: () => apiRequest<Dict[]>("GET", "/generalized-form-engine"),
  });
}

export function useGeneralizedFormLatest() {
  return useQuery(evidenceQuery<AvailablePayload>("generalized-form-latest", "/generalized-form-engine/latest"));
}

export interface PerformancePayload extends AvailablePayload {
  total_runtime_ms?: number;
  average_apply_time_ms?: number;
  average_screening_time_ms?: number;
  average_page_load_ms?: number;
  average_employment_history_time_ms?: number;
  cache_hits?: number;
  cache_misses?: number;
  slowest_operation?: { stage?: string; elapsed_ms?: number } | null;
  evidence_path?: string | null;
}

export function usePerformance() {
  return useQuery(evidenceQuery<PerformancePayload>("performance-latest", "/performance/latest"));
}
