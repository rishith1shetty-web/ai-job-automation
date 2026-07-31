import { describe, expect, it } from "vitest";
import { http, HttpResponse } from "msw";
import { screen, within } from "@testing-library/react";
import { renderWithProviders } from "@/test/render";
import { BASE, fixtures, server } from "@/test/server";
import { DetectedProfilePanel } from "./HeaderPanels";

// A role profile now contains a Primary Role plus up to four Strong Fit roles. The Related and
// Stretch tiers are no longer produced, so their summary groups must disappear rather than sit
// there reading "None" for every resume.
const topFiveSuggestions = [
  { role: "Financial Analyst", confidence: 97, role_category: "primary_role" },
  { role: "FP&A Analyst", confidence: 90, role_category: "strong_fit_roles" },
  { role: "Finance BI Analyst", confidence: 82, role_category: "strong_fit_roles" },
  { role: "Financial Data Analyst", confidence: 81, role_category: "strong_fit_roles" },
  { role: "Revenue / Profitability Analyst", confidence: 74, role_category: "strong_fit_roles" },
];

function seed(role_suggestions: unknown, overrides: Record<string, unknown> = {}) {
  server.use(
    http.get(`${BASE}/resumes/latest`, () =>
      HttpResponse.json({ ...fixtures.resume, role_suggestions, ...overrides }),
    ),
  );
}

/** Render, wait for the panel to settle, and return its three regions.
 *
 * Role names and category labels legitimately appear more than once (headline metric, table,
 * summary strip), so every assertion is scoped to one region rather than to the whole document. */
async function renderPanel() {
  const { container } = renderWithProviders(<DetectedProfilePanel />);
  // A string that appears exactly once, so it is a safe "the panel has rendered" signal.
  await screen.findByText("Generated LinkedIn search query");
  return {
    metrics: container.querySelector("div.grid.grid-cols-1") as HTMLElement,
    table: container.querySelector("table") as HTMLElement,
    strip: container.querySelector("div.grid.grid-cols-2") as HTMLElement,
  };
}

describe("DetectedProfilePanel — role summary groups", () => {
  it("hides Related Roles and Stretch Roles when a profile has only primary and strong fit", async () => {
    seed(topFiveSuggestions);
    const { strip } = await renderPanel();

    expect(within(strip).getByText("Primary Role")).toBeInTheDocument();
    expect(within(strip).getByText("Strong Fit Roles")).toBeInTheDocument();
    expect(within(strip).queryByText("Related Roles")).toBeNull();
    expect(within(strip).queryByText("Stretch Roles")).toBeNull();
    // The empty-group placeholder is gone entirely.
    expect(within(strip).queryByText("None")).toBeNull();
    expect(strip.children.length).toBe(2);
  });

  it("still lists every selected role under its own group", async () => {
    seed(topFiveSuggestions);
    const { strip } = await renderPanel();

    expect(within(strip).getByText("Financial Analyst")).toBeInTheDocument();
    expect(
      within(strip).getByText(
        "FP&A Analyst, Finance BI Analyst, Financial Data Analyst, Revenue / Profitability Analyst",
      ),
    ).toBeInTheDocument();
  });

  it("still renders Related and Stretch groups for an older profile that has them", async () => {
    seed([
      ...topFiveSuggestions.slice(0, 2),
      { role: "Treasury Analyst", confidence: 70, role_category: "related_roles" },
      { role: "Portfolio Analyst", confidence: 61, role_category: "stretch_roles" },
    ]);
    const { strip } = await renderPanel();

    for (const label of ["Primary Role", "Strong Fit Roles", "Related Roles", "Stretch Roles"]) {
      expect(within(strip).getByText(label)).toBeInTheDocument();
    }
    expect(strip.children.length).toBe(4);
  });

  it("hides Strong Fit Roles when the profile is a single primary role", async () => {
    seed([topFiveSuggestions[0]]);
    const { strip } = await renderPanel();

    expect(within(strip).getByText("Primary Role")).toBeInTheDocument();
    expect(within(strip).queryByText("Strong Fit Roles")).toBeNull();
    expect(strip.children.length).toBe(1);
  });

  it("always shows the primary role, even when suggestions carry no category", async () => {
    // An old record predating role categories: the detected primary role is still known.
    seed([{ role: "Data Analyst", confidence: 88 }], { detected_role: "Data Analyst" });
    const { strip } = await renderPanel();

    expect(within(strip).getByText("Primary Role")).toBeInTheDocument();
    expect(within(strip).getByText("Data Analyst")).toBeInTheDocument();
    expect(strip.children.length).toBe(1);
  });

  it("keeps the primary role headline metric and the suggestions table unchanged", async () => {
    seed(topFiveSuggestions);
    const { metrics, table } = await renderPanel();

    // Headline metrics.
    expect(within(metrics).getByText("Primary role")).toBeInTheDocument();
    expect(within(metrics).getByText("Financial Analyst")).toBeInTheDocument();
    expect(within(metrics).getByText("Confidence")).toBeInTheDocument();
    expect(within(metrics).getByText("97%")).toBeInTheDocument();

    // One table row per selected role, ranked 1..5, with its category label.
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows.length).toBe(topFiveSuggestions.length);
    expect(within(rows[0]).getByText("1")).toBeInTheDocument();
    expect(within(rows[0]).getByText("Primary Role")).toBeInTheDocument();
    expect(within(rows[4]).getByText("5")).toBeInTheDocument();
    expect(within(rows[4]).getByText("Revenue / Profitability Analyst")).toBeInTheDocument();
  });
});
