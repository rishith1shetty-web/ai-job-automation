import { describe, expect, it } from "vitest";
import { http, HttpResponse } from "msw";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BASE, server } from "@/test/server";
import App from "./App";

// Removing sections from the navigation must not affect routing: every route still
// resolves to its own page, reachable by direct URL.
const evidence = { available: false };

function seedShellAndEvidence() {
  server.use(
    http.get(`${BASE}/health`, () => HttpResponse.json({ status: "ok" })),
    http.get(`${BASE}/jobs/search-batches`, () => HttpResponse.json([])),
    http.get(`${BASE}/resume-intelligence/latest`, () => HttpResponse.json(evidence)),
    http.get(`${BASE}/reasoning/latest`, () => HttpResponse.json(evidence)),
    http.get(`${BASE}/form-learning`, () => HttpResponse.json([])),
    http.get(`${BASE}/form-learning/latest`, () => HttpResponse.json(evidence)),
    http.get(`${BASE}/generalized-form-engine`, () => HttpResponse.json([])),
    http.get(`${BASE}/generalized-form-engine/latest`, () => HttpResponse.json(evidence)),
    http.get(`${BASE}/performance/latest`, () => HttpResponse.json(evidence)),
  );
}

// App owns its BrowserRouter, so the entry URL is set on the jsdom history.
function renderAppAt(path: string) {
  seedShellAndEvidence();
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

describe("App routes — direct URL access is preserved", () => {
  it.each([
    ["/ai-reasoning", "AI reasoning"],
    ["/form-learning", "AI Form Learning"],
    ["/generalized-form", "Generalized Form Engine"],
  ])("loads %s directly even though it is no longer in the navigation", async (path, heading) => {
    renderAppAt(path);
    expect(await screen.findByRole("heading", { name: heading })).toBeInTheDocument();
    expect(window.location.pathname).toBe(path);
  });

  it.each([
    ["/matched-jobs", "Matched jobs"],
    ["/applications", "Application history"],
    ["/performance", "Performance"],
  ])("still loads the primary section %s", async (path, heading) => {
    renderAppAt(path);
    expect(await screen.findByRole("heading", { name: heading })).toBeInTheDocument();
  });

  it("still redirects an unknown route to Matched jobs", async () => {
    renderAppAt("/does-not-exist");
    expect(await screen.findByRole("heading", { name: "Matched jobs" })).toBeInTheDocument();
    expect(window.location.pathname).toBe("/matched-jobs");
  });
});
