import { lazy } from "react";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { SessionProvider } from "@/store/session";
import { AppShell } from "@/layouts/AppShell";

// Each section is code-split into its own chunk (loaded on first navigation to that
// route) so the initial download only carries the shell + the landing page. The pages
// use named exports, so map them to the default export React.lazy expects.
const MatchedJobsPage = lazy(() => import("@/pages/MatchedJobsPage").then((m) => ({ default: m.MatchedJobsPage })));
const ApplicationHistoryPage = lazy(() => import("@/pages/ApplicationHistoryPage").then((m) => ({ default: m.ApplicationHistoryPage })));
const ProfilePage = lazy(() => import("@/pages/ProfilePage").then((m) => ({ default: m.ProfilePage })));
const EmploymentHistoryPage = lazy(() => import("@/pages/EmploymentHistoryPage").then((m) => ({ default: m.EmploymentHistoryPage })));
const ScreeningKnowledgePage = lazy(() => import("@/pages/ScreeningKnowledgePage").then((m) => ({ default: m.ScreeningKnowledgePage })));
const AIReasoningPage = lazy(() => import("@/pages/AIReasoningPage").then((m) => ({ default: m.AIReasoningPage })));
const FormLearningPage = lazy(() => import("@/pages/FormLearningPage").then((m) => ({ default: m.FormLearningPage })));
const GeneralizedFormEnginePage = lazy(() => import("@/pages/GeneralizedFormEnginePage").then((m) => ({ default: m.GeneralizedFormEnginePage })));
const PerformancePage = lazy(() => import("@/pages/PerformancePage").then((m) => ({ default: m.PerformancePage })));

export default function App() {
  return (
    <SessionProvider>
      <BrowserRouter>
        <Routes>
          <Route element={<AppShell />}>
            <Route index element={<Navigate to="/matched-jobs" replace />} />
            <Route path="/matched-jobs" element={<MatchedJobsPage />} />
            <Route path="/applications" element={<ApplicationHistoryPage />} />
            <Route path="/profile" element={<ProfilePage />} />
            <Route path="/employment-history" element={<EmploymentHistoryPage />} />
            <Route path="/screening" element={<ScreeningKnowledgePage />} />
            <Route path="/ai-reasoning" element={<AIReasoningPage />} />
            <Route path="/form-learning" element={<FormLearningPage />} />
            <Route path="/generalized-form" element={<GeneralizedFormEnginePage />} />
            <Route path="/performance" element={<PerformancePage />} />
            <Route path="*" element={<Navigate to="/matched-jobs" replace />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </SessionProvider>
  );
}
