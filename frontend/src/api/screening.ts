import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiRequest } from "@/lib/api";
import type { ScreeningAnswer, ScreeningSetting } from "@/types";

// Phase 4.3 read (screening_mode for apply) + Phase 4.4 settings/knowledge CRUD.
// Endpoints (unchanged contract):
//   GET/PUT /screening/settings
//   GET/POST /screening/knowledge, DELETE /screening/knowledge/{id}
export const screeningSettingsKey = ["screening", "settings"] as const;
export const screeningKnowledgeKey = ["screening", "knowledge"] as const;

export function useScreeningSettings() {
  return useQuery({
    queryKey: screeningSettingsKey,
    queryFn: () => apiRequest<ScreeningSetting>("GET", "/screening/settings"),
  });
}

export interface ScreeningSettingsInput {
  mode: "safe" | "aggressive";
  notice_period_default: string;
}

export function useSaveScreeningSettings() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: ScreeningSettingsInput) =>
      apiRequest<ScreeningSetting>("PUT", "/screening/settings", { body: input }),
    onSuccess: () => queryClient.invalidateQueries(),
  });
}

export function useScreeningKnowledge() {
  return useQuery({
    queryKey: screeningKnowledgeKey,
    queryFn: () => apiRequest<ScreeningAnswer[]>("GET", "/screening/knowledge"),
  });
}

// POST /screening/knowledge — currency "" becomes null (not empty string), matching
// dashboard.py's `currency or None`; evidence is wrapped as { note: ... }.
export interface ScreeningAnswerInput {
  question_text: string;
  category: string;
  answer: string;
  source: string;
  confidence: string;
  currency: string | null;
  evidence: { note: string };
  resume_id: number | null;
}

export function useSaveScreeningAnswer() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: ScreeningAnswerInput) =>
      apiRequest<ScreeningAnswer>("POST", "/screening/knowledge", { body: input }),
    onSuccess: () => queryClient.invalidateQueries(),
  });
}

export function useDeleteScreeningAnswer() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => apiRequest("DELETE", `/screening/knowledge/${id}`),
    onSuccess: () => queryClient.invalidateQueries(),
  });
}
