import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, apiRequest, apiUpload } from "@/lib/api";
import type { CandidateProfile, ResumeProfile } from "@/types";

// Phase 4.1: real backend wiring for the resume + candidate-profile modules only.
// These are the ONLY live operational endpoints in the app; everything else is still
// fixture-backed. Endpoints used (unchanged from the Phase 1 contract):
//   GET  /resumes/latest    -> latest analyzed resume (404 when none)
//   GET  /candidate-profile -> get-or-create candidate profile (never 404)
//   POST /resumes           -> upload + analyze in one call, returns ResumeProfileOut

export const resumeKeys = { latest: ["resume", "latest"] as const };
export const candidateProfileKey = ["candidate-profile"] as const;

/** Latest resume for the active portal. Returns null (not an error) when none exists. */
export function useLatestResume() {
  return useQuery({
    queryKey: resumeKeys.latest,
    queryFn: async (): Promise<ResumeProfile | null> => {
      try {
        return await apiRequest<ResumeProfile>("GET", "/resumes/latest");
      } catch (err) {
        if (err instanceof ApiError && err.status === 404) return null;
        throw err;
      }
    },
  });
}

export function useCandidateProfile() {
  return useQuery({
    queryKey: candidateProfileKey,
    queryFn: () => apiRequest<CandidateProfile>("GET", "/candidate-profile"),
  });
}

// PUT /candidate-profile — the 12 keys dashboard.py sends, in the same order, with NO
// portal field (the backend resolves the active portal itself).
export interface CandidateProfileInput {
  first_name: string;
  last_name: string;
  email: string;
  mobile_phone: string;
  phone_country_code: string;
  current_location: string;
  linkedin_url: string;
  portfolio_url: string;
  github_url: string;
  current_job_title: string;
  current_company: string;
  professional_headline: string;
}

export function useSaveCandidateProfile() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: CandidateProfileInput) =>
      apiRequest<CandidateProfile>("PUT", "/candidate-profile", { body: input }),
    onSuccess: () => queryClient.invalidateQueries(), // mirror clear_cached_reads()
  });
}

/** Upload + analyze a resume. Mirrors the Streamlit "Analyze resume" button, which
 *  POSTs the file to /resumes and then refreshes dashboard reads. */
export function useUploadResume() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (file: File): Promise<ResumeProfile> => {
      const formData = new FormData();
      formData.append("upload", file, file.name);
      return apiUpload<ResumeProfile>("/resumes", formData);
    },
    onSuccess: (data) => {
      // Seed the cache so the header/KPI update instantly, then invalidate so the
      // candidate profile (auto-filled during upload) refetches too.
      queryClient.setQueryData(resumeKeys.latest, data);
      queryClient.invalidateQueries({ queryKey: resumeKeys.latest });
      queryClient.invalidateQueries({ queryKey: candidateProfileKey });
    },
  });
}
