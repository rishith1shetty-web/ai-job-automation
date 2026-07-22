import { createContext, useCallback, useContext, useMemo, useState } from "react";

// Cross-component UI state that isn't server data: which search batch is selected, plus a
// nonce that New Session bumps to force a cold-start remount of the routed page.
// The batch selector (shell SearchBatchBar), the jobs table (MatchedJobsPage) and the
// KPI summary all read the same selection — mirroring Streamlit's session_state
// selected_batch_id / new_search_batch_id. Portal and resume come from TanStack Query
// caches, so they don't live here.
interface SessionState {
  selectedBatchId: string | null;
  setSelectedBatchId: (id: string | null) => void;
  // Incremented by New Session. The routed area keys on it so a reset remounts the current
  // page, dropping its local component state (dry-run result view, selected job, apply-run
  // pointer, search-form choices) that a re-render alone would keep. Server data is refreshed
  // separately by resetting the TanStack Query cache.
  sessionNonce: number;
  resetSession: () => void;
}

const SessionContext = createContext<SessionState | null>(null);

export function SessionProvider({ children }: { children: React.ReactNode }) {
  const [selectedBatchId, setSelectedBatchId] = useState<string | null>(null);
  const [sessionNonce, setSessionNonce] = useState(0);
  const resetSession = useCallback(() => {
    setSelectedBatchId(null);
    setSessionNonce((n) => n + 1);
  }, []);
  const value = useMemo(
    () => ({ selectedBatchId, setSelectedBatchId, sessionNonce, resetSession }),
    [selectedBatchId, sessionNonce, resetSession],
  );
  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

export function useSession(): SessionState {
  const ctx = useContext(SessionContext);
  if (!ctx) throw new Error("useSession must be used within SessionProvider");
  return ctx;
}
