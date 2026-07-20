import { describe, expect, it } from "vitest";
import { postedTier } from "./postedAge";

// Display-only classification of the raw portal posted text. Never affects sorting or data.
describe("postedTier", () => {
  it("strongest emphasis for just-now values", () => {
    for (const text of ["Just now", "moments ago", "Moment ago", "30 seconds ago"]) {
      expect(postedTier(text)).toBe("instant");
    }
  });

  it("highlights minute-level ages", () => {
    for (const text of ["5 min ago", "7 minutes ago", "Posted 12 minutes ago"]) {
      expect(postedTier(text)).toBe("minutes");
    }
  });

  it("normal emphasis for hour-level ages", () => {
    for (const text of ["2 hours ago", "1 hr ago", "Posted 23 hours ago"]) {
      expect(postedTier(text)).toBe("hours");
    }
  });

  it("today is visible but distinct from minutes/hours", () => {
    expect(postedTier("Today")).toBe("today");
    expect(postedTier("Posted today")).toBe("today");
  });

  it("neutral for yesterday and older ages", () => {
    for (const text of ["Yesterday", "3 days ago", "1 week ago", "2 months ago",
                        "30+ days ago", "2026-07-15", "July 15, 2026"]) {
      expect(postedTier(text)).toBe("older");
    }
  });

  it("muted for missing or unrecognized values", () => {
    for (const text of ["", "   ", null, undefined, "N/A", "soon"]) {
      expect(postedTier(text)).toBe("unknown");
    }
  });
});
