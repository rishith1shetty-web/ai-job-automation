import { describe, expect, it } from "vitest";
import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { MainNav } from "./MainNav";

// MainNav issues no requests, so it only needs a router.
function renderNav(route = "/matched-jobs") {
  return render(
    <MemoryRouter initialEntries={[route]}>
      <MainNav />
    </MemoryRouter>,
  );
}

const NAV = () => screen.getByRole("navigation", { name: "Dashboard sections" });

// Every section removed from the navigation, by its former label.
const REMOVED = [
  "Profile",
  "Employment History",
  "Screening Knowledge",
  "Performance",
  "AI Reasoning",
  "AI Form Learning",
  "Generalized Form Engine",
];

describe("MainNav — two-tab navigation", () => {
  it("3 + 4. shows exactly Matched jobs and Application history, in that order", () => {
    renderNav();
    const labels = within(NAV())
      .getAllByRole("link")
      .map((link) => link.textContent);
    expect(labels).toEqual(["Matched jobs", "Application history"]);
  });

  it("keeps the existing routes on both tabs", () => {
    renderNav();
    const href = (name: string) => screen.getByRole("link", { name }).getAttribute("href");
    expect(href("Matched jobs")).toBe("/matched-jobs");
    expect(href("Application history")).toBe("/applications");
  });

  it("5-11. no removed section appears in the navigation, as a link or as any other text", () => {
    renderNav();
    for (const label of REMOVED) {
      expect(screen.queryByRole("link", { name: label })).toBeNull();
      // Not merely un-linked: the label must not be rendered as a placeholder either.
      expect(within(NAV()).queryByText(label)).toBeNull();
    }
  });

  it("12. has no Advanced button or disclosure group", () => {
    renderNav();
    expect(screen.queryByRole("button", { name: /advanced/i })).toBeNull();
    // The whole nav is now non-interactive apart from its two links.
    expect(within(NAV()).queryAllByRole("button")).toHaveLength(0);
    expect(document.querySelector("#advanced-nav-group")).toBeNull();
  });

  it("marks the current section active (aria-current) as before", () => {
    renderNav("/applications");
    expect(screen.getByRole("link", { name: "Application history" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "Matched jobs" })).not.toHaveAttribute("aria-current");
  });

  it("is keyboard reachable — both tabs are in the tab order and nothing else is", () => {
    renderNav();
    const focusable = within(NAV()).getAllByRole("link");
    expect(focusable).toHaveLength(2);
    focusable.forEach((link) => expect(link).not.toHaveAttribute("tabindex", "-1"));
  });

  it("shrink-wraps its two pills instead of stretching the old full-width bar", () => {
    renderNav();
    expect(NAV().className).toContain("w-fit");
  });
});
