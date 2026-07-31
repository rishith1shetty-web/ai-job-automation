/**
 * Whole-dashboard proofs for the product wording and the two-tab navigation.
 *
 * MainNav.test.tsx pins the nav component in isolation; this file renders the real App
 * (Sidebar + TopNav + WelcomeCard + header panels + MainNav + the routed page) so the
 * branding and the default view are asserted against what a user actually sees.
 */
import { describe, expect, it } from "vitest";
import { http, HttpResponse } from "msw";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BASE, server } from "@/test/server";
import App from "./App";

const evidence = { available: false };

function renderDashboardAt(path: string) {
  server.use(
    http.get(`${BASE}/health`, () => HttpResponse.json({ status: "ok" })),
    http.get(`${BASE}/jobs/search-batches`, () => HttpResponse.json([])),
    http.get(`${BASE}/resume-intelligence/latest`, () => HttpResponse.json(evidence)),
    http.get(`${BASE}/reasoning/latest`, () => HttpResponse.json(evidence)),
    http.get(`${BASE}/form-learning/latest`, () => HttpResponse.json(evidence)),
    http.get(`${BASE}/generalized-form-engine/latest`, () => HttpResponse.json(evidence)),
    http.get(`${BASE}/performance/latest`, () => HttpResponse.json(evidence)),
  );
  window.history.pushState({}, "", path);
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0, staleTime: 0 } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>,
  );
}

const nav = () => screen.getByRole("navigation", { name: "Dashboard sections" });

describe("Product wording", () => {
  it("1. shows 'Job Application Platform' in the dashboard branding", async () => {
    renderDashboardAt("/matched-jobs");
    await screen.findByRole("heading", { name: "Matched jobs" });
    // Sidebar subtitle, top header subtitle and welcome banner badge all carry it.
    expect(screen.getAllByText("Job Application Platform").length).toBeGreaterThanOrEqual(3);
  });

  it("2. never renders 'AI Recruitment Automation Platform' anywhere in the dashboard", async () => {
    renderDashboardAt("/matched-jobs");
    await screen.findByRole("heading", { name: "Matched jobs" });
    const rendered = document.body.textContent ?? "";
    expect(rendered).not.toContain("AI Recruitment Automation Platform");
    // The shorter legacy variants used by the sidebar and top header are gone too.
    expect(rendered).not.toContain("AI Recruitment Automation");
    expect(rendered).not.toContain("AI Recruitment Platform");
  });
});

describe("Dashboard navigation", () => {
  it("3 + 4. offers Matched jobs and Application history, and nothing else", async () => {
    renderDashboardAt("/matched-jobs");
    await screen.findByRole("heading", { name: "Matched jobs" });
    const labels = within(nav())
      .getAllByRole("link")
      .map((link) => link.textContent);
    expect(labels).toEqual(["Matched jobs", "Application history"]);
  });

  it.each([
    ["5", "Profile"],
    ["6", "Employment History"],
    ["7", "Screening Knowledge"],
    ["8", "Performance"],
    ["9", "AI Reasoning"],
    ["10", "AI Form Learning"],
    ["11", "Generalized Form Engine"],
  ])("%s. %s is not in the navigation", async (_n, label) => {
    renderDashboardAt("/matched-jobs");
    await screen.findByRole("heading", { name: "Matched jobs" });
    // Scoped to the nav on purpose: "Profile" still legitimately labels a sidebar
    // section, and the removal is about reachability from the navigation bar.
    expect(within(nav()).queryByText(label)).toBeNull();
    expect(within(nav()).queryByRole("link", { name: label })).toBeNull();
  });

  it("12. the Advanced dropdown is gone from the dashboard", async () => {
    renderDashboardAt("/matched-jobs");
    await screen.findByRole("heading", { name: "Matched jobs" });
    expect(screen.queryByRole("button", { name: /advanced/i })).toBeNull();
    expect(within(nav()).queryAllByRole("button")).toHaveLength(0);
  });

  it("13. Matched jobs is the default view", async () => {
    renderDashboardAt("/");
    expect(await screen.findByRole("heading", { name: "Matched jobs" })).toBeInTheDocument();
    expect(window.location.pathname).toBe("/matched-jobs");
    expect(screen.getByRole("link", { name: "Matched jobs" })).toHaveAttribute("aria-current", "page");
  });

  it("14. Application history can still be opened from the navigation", async () => {
    const user = userEvent.setup();
    renderDashboardAt("/matched-jobs");
    await screen.findByRole("heading", { name: "Matched jobs" });

    await user.click(screen.getByRole("link", { name: "Application history" }));

    expect(await screen.findByRole("heading", { name: "Application history" })).toBeInTheDocument();
    expect(window.location.pathname).toBe("/applications");
    expect(screen.getByRole("link", { name: "Application history" })).toHaveAttribute("aria-current", "page");
  });
});
