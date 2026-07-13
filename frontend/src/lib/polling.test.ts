import { describe, expect, it } from "vitest";
import { pollWhileActive } from "./polling";

// Search + apply batch polling lifecycle: 2s while active, stop on terminal state.
describe("pollWhileActive", () => {
  it("polls every 2000ms while queued or running", () => {
    expect(pollWhileActive("queued")).toBe(2000);
    expect(pollWhileActive("running")).toBe(2000);
  });

  it("stops polling on a terminal or unknown status", () => {
    expect(pollWhileActive("completed")).toBe(false);
    expect(pollWhileActive("failed")).toBe(false);
    expect(pollWhileActive(undefined)).toBe(false);
    expect(pollWhileActive(null)).toBe(false);
  });
});
