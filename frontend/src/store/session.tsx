import { createContext, useContext, useMemo, useState } from "react";

// Cross-component UI state that isn't server data: which search batch is selected.
// The batch selector (shell SearchBatchBar), the jobs table (MatchedJobsPage) and the
// KPI summary all read the same selection — mirroring Streamlit's session_state
// selected_batch_id / new_search_batch_id. Portal and resume come from TanStack Query
// caches, so they don't live here.
interface SessionState {
  selectedBatchId: string | null;
  setSelectedBatchId: (id: string | null) => void;
}

const SessionContext = createContext<SessionState | null>(null);

export function SessionProvider({ children }: { children: React.ReactNode }) {
  const [selectedBatchId, setSelectedBatchId] = useState<string | null>(null);
  const value = useMemo(() => ({ selectedBatchId, setSelectedBatchId }), [selectedBatchId]);
  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

export function useSession(): SessionState {
  const ctx = useContext(SessionContext);
  if (!ctx) throw new Error("useSession must be used within SessionProvider");
  return ctx;
}
