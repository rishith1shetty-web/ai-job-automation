import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { SessionProvider } from "@/store/session";
import { AppShell } from "@/layouts/AppShell";
import { MatchedJobsPage } from "@/pages/MatchedJobsPage";
import { ApplicationHistoryPage } from "@/pages/ApplicationHistoryPage";
import { ProfilePage } from "@/pages/ProfilePage";
import { EmploymentHistoryPage } from "@/pages/EmploymentHistoryPage";
import { ScreeningKnowledgePage } from "@/pages/ScreeningKnowledgePage";
import { AIReasoningPage } from "@/pages/AIReasoningPage";
import { FormLearningPage } from "@/pages/FormLearningPage";
import { GeneralizedFormEnginePage } from "@/pages/GeneralizedFormEnginePage";
import { PerformancePage } from "@/pages/PerformancePage";

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
