import { Suspense } from "react";
import { Outlet, useLocation } from "react-router-dom";
import { useSession } from "@/store/session";
import { Sidebar } from "@/components/shell/Sidebar";
import { TopNav } from "@/components/shell/TopNav";
import { WelcomeCard } from "@/components/shell/WelcomeCard";
import { ResumeSummaryStrip, DetectedProfilePanel, SearchBatchBar } from "@/components/shell/HeaderPanels";
import { KpiGrid } from "@/components/shell/KpiGrid";
import { MainNav } from "@/components/shell/MainNav";
import { Skeleton } from "@/components/ui/skeleton";

// Only this small component subscribes to the location, so navigating between sections
// re-renders just the routed area — not the whole persistent shell (Sidebar, TopNav,
// KPIs, etc.). Suspense shows a skeleton while a lazily-loaded page chunk downloads.
function RoutedOutlet() {
  const location = useLocation();
  const { sessionNonce } = useSession();
  return (
    <main className="pb-10">
      <Suspense fallback={<Skeleton className="h-64" />}>
        {/* Keyed on the path AND the session nonce: navigating remounts the page, and so does
            New Session -- clearing any local component state (dry-run result, apply-run pointer,
            selected job, form choices) so the dashboard truly cold-starts. */}
        <div key={`${location.pathname}:${sessionNonce}`} className="animate-fade-in">
          <Outlet />
        </div>
      </Suspense>
    </main>
  );
}

// Persistent shell. Everything above <Outlet/> stays visible on every route, matching
// the Streamlit dashboard where the header stack renders before the section radio.
export function AppShell() {
  return (
    <div className="flex min-h-dvh bg-parchment-bg">
      <Sidebar />
      <div className="min-w-0 flex-1">
        <div className="mx-auto max-w-[1440px] space-y-4 px-4 py-4 sm:px-6">
          <TopNav />
          <WelcomeCard />
          <ResumeSummaryStrip />
          <DetectedProfilePanel />
          <SearchBatchBar />
          <KpiGrid />
          <MainNav />
          <RoutedOutlet />
        </div>
      </div>
    </div>
  );
}
