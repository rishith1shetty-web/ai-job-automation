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
