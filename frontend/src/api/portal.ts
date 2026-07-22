import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiRequest } from "@/lib/api";
import type { Portal, PortalStatus } from "@/types";

// Phase 4.2: portal module wired to the real backend. Endpoints (unchanged contract):
//   GET  /portal/active           -> { active_portal, linkedin_connected, dice_connected }
//   POST /portal/active/{portal}  -> switch active portal (disconnects the other)
//   POST /linkedin/connect | /linkedin/disconnect
//   POST /dice/connect     | /dice/disconnect
//
// Every mutation invalidates ALL queries, mirroring dashboard.py's clear_cached_reads():
// switching or (dis)connecting a portal changes which resume/profile/jobs are in scope.

export const portalKey = ["portal", "active"] as const;

export function usePortalStatus() {
  return useQuery({
    queryKey: portalKey,
    queryFn: () => apiRequest<PortalStatus>("GET", "/portal/active"),
  });
}

export function useSwitchPortal() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (portal: Portal) => apiRequest("POST", `/portal/active/${portal}`),
    onSuccess: () => queryClient.invalidateQueries(),
  });
}

export function useConnectLinkedIn() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => apiRequest("POST", "/linkedin/connect"),
    onSuccess: () => queryClient.invalidateQueries(),
  });
}

export function useDisconnectLinkedIn() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => apiRequest("POST", "/linkedin/disconnect"),
    onSuccess: () => queryClient.invalidateQueries(),
  });
}

export function useConnectDice() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => apiRequest("POST", "/dice/connect"),
    onSuccess: () => queryClient.invalidateQueries(),
  });
}

export function useDisconnectDice() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => apiRequest("POST", "/dice/disconnect"),
    onSuccess: () => queryClient.invalidateQueries(),
  });
}

// New Session: start a fresh dashboard without restarting the backend.
//   POST /session/reset -> resets ONLY the in-memory RuntimeSession (no DB/file/automation
//   change) and returns the clean session state.
// resetQueries() returns EVERY cached query to its initial (blank) state and refetches the
// active ones. Unlike clear(), it notifies each query's observers, so components that would
// not otherwise re-render -- the TopNav portal badges and the resume strip -- also repaint
// from the fresh backend reads (resume 404 -> empty, portals offline). No manual refresh, and
// no stale data lingering. Historical endpoints simply re-read the DB, so history is intact.
export function useResetSession() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => apiRequest("POST", "/session/reset"),
    onSuccess: async () => {
      await queryClient.resetQueries();
    },
  });
}
