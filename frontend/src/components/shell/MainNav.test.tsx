import { describe, expect, it } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
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

const PRIMARY = [
  "Matched jobs",
  "Application history",
  "Profile",
  "Employment History",
  "Screening Knowledge",
  "Performance",
];
const ADVANCED = ["AI Reasoning", "AI Form Learning", "Generalized Form Engine"];

const advancedToggle = () => screen.getByRole("button", { name: /advanced/i });

describe("MainNav — recruiter-facing primary navigation", () => {
  it("shows the six recruiter sections in the required order", () => {
    renderNav();
    const nav = screen.getByRole("navigation", { name: "Dashboard sections" });
    const labels = within(nav)
      .getAllByRole("link")
      .map((link) => link.textContent);
    expect(labels).toEqual(PRIMARY);
  });

  it("does not show the technical sections as primary navigation items", () => {
    renderNav();
    for (const label of ADVANCED) {
      expect(screen.queryByRole("link", { name: label })).toBeNull();
    }
  });

  it("keeps the existing routes on every primary item", () => {
    renderNav();
    const href = (name: string) => screen.getByRole("link", { name }).getAttribute("href");
    expect(href("Matched jobs")).toBe("/matched-jobs");
    expect(href("Application history")).toBe("/applications");
    expect(href("Profile")).toBe("/profile");
    expect(href("Employment History")).toBe("/employment-history");
    expect(href("Screening Knowledge")).toBe("/screening");
    expect(href("Performance")).toBe("/performance");
  });

  it("marks the current section active (aria-current) as before", () => {
    renderNav("/screening");
    expect(screen.getByRole("link", { name: "Screening Knowledge" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "Profile" })).not.toHaveAttribute("aria-current");
  });
});

describe("MainNav — Advanced group", () => {
  it("is collapsed by default", () => {
    renderNav();
    expect(advancedToggle()).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("link", { name: "AI Reasoning" })).toBeNull();
  });

  it("expands to reveal the three technical sections with their exact names and routes", async () => {
    const user = userEvent.setup();
    renderNav();
    await user.click(advancedToggle());

    expect(advancedToggle()).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("link", { name: "AI Reasoning" })).toHaveAttribute("href", "/ai-reasoning");
    expect(screen.getByRole("link", { name: "AI Form Learning" })).toHaveAttribute("href", "/form-learning");
    expect(screen.getByRole("link", { name: "Generalized Form Engine" })).toHaveAttribute("href", "/generalized-form");
  });

  it("collapses again on a second activation", async () => {
    const user = userEvent.setup();
    renderNav();
    await user.click(advancedToggle());
    await user.click(advancedToggle());

    expect(advancedToggle()).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("link", { name: "AI Reasoning" })).toBeNull();
  });

  it("is keyboard accessible — focusable by Tab and toggled by Enter and Space", async () => {
    const user = userEvent.setup();
    renderNav();

    // Six primary links come first in tab order, then the toggle.
    for (let i = 0; i < PRIMARY.length + 1; i += 1) await user.tab();
    expect(advancedToggle()).toHaveFocus();

    await user.keyboard("{Enter}");
    expect(advancedToggle()).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("link", { name: "AI Form Learning" })).toBeInTheDocument();

    await user.keyboard(" ");
    expect(advancedToggle()).toHaveAttribute("aria-expanded", "false");
  });

  it("starts expanded when the current route is a technical section, so a direct URL never hides the active item", () => {
    renderNav("/generalized-form");
    expect(advancedToggle()).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("link", { name: "Generalized Form Engine" })).toHaveAttribute("aria-current", "page");
  });
});
