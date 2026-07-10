import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiRequest } from "@/lib/api";
import type { EmploymentHistory } from "@/types";

// Phase 4.4: employment history CRUD. Endpoints (unchanged contract):
//   GET    /employment-history?resume_id=
//   POST   /employment-history
//   PUT    /employment-history/{id}
//   DELETE /employment-history/{id}

export const employmentKey = ["employment-history"] as const;

export function useEmploymentHistory(resumeId?: number) {
  return useQuery({
    queryKey: [...employmentKey, resumeId ?? null],
    queryFn: () =>
      apiRequest<EmploymentHistory[]>(
        "GET",
        resumeId != null ? `/employment-history?resume_id=${resumeId}` : "/employment-history",
      ),
  });
}

// Payload keys and serialization match dashboard.py exactly: currently_work_here forces
// end_month/end_year to null; skills/technologies are comma-split + trimmed.
export interface EmploymentInput {
  resume_id: number | null;
  display_order: number;
  company_name: string;
  job_title: string;
  city: string;
  state: string;
  country: string;
  employment_type: string;
  start_month: string;
  start_year: number | null;
  end_month: string | null;
  end_year: number | null;
  currently_work_here: boolean;
  description: string;
  skills_used: string[];
  technologies_used: string[];
  confidence: number;
  source: string;
}

/** POST when no id (create), PUT when id present (update). */
export function useSaveEmployment() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, payload }: { id: number | null; payload: EmploymentInput }) =>
      id != null
        ? apiRequest<EmploymentHistory>("PUT", `/employment-history/${id}`, { body: payload })
        : apiRequest<EmploymentHistory>("POST", "/employment-history", { body: payload }),
    onSuccess: () => queryClient.invalidateQueries(),
  });
}

export function useDeleteEmployment() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => apiRequest("DELETE", `/employment-history/${id}`),
    onSuccess: () => queryClient.invalidateQueries(),
  });
}
