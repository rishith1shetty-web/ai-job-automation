import { useQuery } from "@tanstack/react-query";
import { Logo } from "@/components/ui/Logo";
import { Badge } from "@/components/ui/badge";
import { apiRequest } from "@/lib/api";
import { usePortalStatus } from "@/api/portal";

// Health ping carried over from Phase 2 — read-only, not an operational endpoint.
function useHealth() {
  return useQuery({
    queryKey: ["health"],
    queryFn: () => apiRequest<{ status: string }>("GET", "/health"),
    retry: 0,
  });
}

export function TopNav() {
  const { data: status } = usePortalStatus(); // LIVE: GET /portal/active
  const health = useHealth();
  const activePortal = status?.active_portal ?? "linkedin";
  const linkedinConnected = Boolean(status?.linkedin_connected);
  const diceConnected = Boolean(status?.dice_connected);
  const portalLabel = activePortal.charAt(0).toUpperCase() + activePortal.slice(1);

  return (
    <header className="flex flex-wrap items-center gap-3 rounded-lg border border-parchment-border bg-card px-4 py-2.5 shadow-card">
      <Logo size={40} />
      <div className="leading-tight">
        <div className="text-[1.05rem] font-extrabold tracking-tight text-ink">Rising Path Recruiters</div>
        <div className="text-[.72rem] font-semibold uppercase tracking-[.08em] text-gold-text">
          AI Recruitment Automation
        </div>
      </div>
      <div className="flex-1" />
      <div className="flex flex-wrap items-center gap-2 sm:border-l sm:border-parchment-border sm:pl-3" role="status" aria-label="Portal and backend status">
        <Badge variant="gold" dot>
          Active portal: {portalLabel}
        </Badge>
        <Badge variant={linkedinConnected ? "success" : "muted"} dot>
          {linkedinConnected ? "LinkedIn connected" : "LinkedIn offline"}
        </Badge>
        <Badge variant={diceConnected ? "success" : "muted"} dot>
          {diceConnected ? "Dice connected" : "Dice offline"}
        </Badge>
        <Badge variant={health.isError ? "danger" : health.isLoading ? "muted" : "success"} dot>
          {health.isError ? "Backend unreachable" : health.isLoading ? "Checking backend…" : "Backend online"}
        </Badge>
      </div>
    </header>
  );
}
