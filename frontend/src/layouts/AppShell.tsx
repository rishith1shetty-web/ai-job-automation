import { Outlet } from "react-router-dom";
import { Sidebar } from "@/components/shell/Sidebar";
import { TopNav } from "@/components/shell/TopNav";
import { WelcomeCard } from "@/components/shell/WelcomeCard";
import { ResumeSummaryStrip, DetectedProfilePanel, SearchBatchBar } from "@/components/shell/HeaderPanels";
import { KpiGrid } from "@/components/shell/KpiGrid";
import { MainNav } from "@/components/shell/MainNav";

// Persistent shell. Everything above <Outlet/> stays visible on every route, matching
// the Streamlit dashboard where the header stack renders before the section radio.
export function AppShell() {
  return (
    <div className="flex min-h-dvh bg-parchment-bg">
      <Sidebar />
      <div className="min-w-0 flex-1">
        <div className="mx-auto max-w-[1440px] space-y-4 px-6 py-4">
          <TopNav />
          <WelcomeCard />
          <ResumeSummaryStrip />
          <DetectedProfilePanel />
          <SearchBatchBar />
          <KpiGrid />
          <MainNav />
          <main className="pb-10">
            <Outlet />
          </main>
        </div>
      </div>
    </div>
  );
}
