import { describe, expect, it } from "vitest";
import { classifyApplyStatus, summarizeApplyRun } from "./applyOutcome";

describe("classifyApplyStatus", () => {
  it("treats live submissions and submit-ready dry runs as success", () => {
    for (const s of ["applied", "reached_review", "submitted", "dry_run_ready", "submit_ready"]) {
      expect(classifyApplyStatus(s)).toBe("success");
    }
    expect(classifyApplyStatus("APPLIED")).toBe("success"); // case-insensitive
  });

  it("treats every other terminal status as failure", () => {
    for (const s of ["failed", "skipped", "needs_review", "unsupported_form", "timed_out", "external_application", "missing_required_answer"]) {
      expect(classifyApplyStatus(s)).toBe("failed");
    }
  });

  it("never classifies queued/running jobs as terminal", () => {
    for (const s of ["queued", "running", "matched", "pending", "in_progress", ""]) {
      expect(classifyApplyStatus(s)).toBe("pending");
    }
    expect(classifyApplyStatus(undefined)).toBe("pending");
  });
});

describe("summarizeApplyRun", () => {
  it("computes attempted = successful + failed from per-job results", () => {
    const results = [
      { status: "applied" }, // success
      { status: "reached_review" }, // success (dry-run submit-ready)
      { status: "failed" }, // failure
      { status: "skipped" }, // failure
      { status: "needs_review" }, // failure
      { status: "timed_out" }, // failure
      { status: "queued" }, // NOT counted
    ];
    const s = summarizeApplyRun(results);
    expect(s.successful).toBe(2);
    expect(s.attempted).toBe(6); // queued excluded
    expect(s.failed).toBe(4); // attempted - successful, each failure counted once
    expect(s.successRate).toBe(33); // round(2/6*100)
  });

  it("returns 0% when nothing has been attempted", () => {
    expect(summarizeApplyRun([])).toEqual({ attempted: 0, successful: 0, failed: 0, successRate: 0 });
    expect(summarizeApplyRun(undefined)).toEqual({ attempted: 0, successful: 0, failed: 0, successRate: 0 });
    // Only non-terminal jobs -> still 0 attempted (not counted as failures).
    expect(summarizeApplyRun([{ status: "running" }, { status: "queued" }])).toEqual({
      attempted: 0,
      successful: 0,
      failed: 0,
      successRate: 0,
    });
  });

  it("reaches 100% when every attempted job succeeded", () => {
    expect(summarizeApplyRun([{ status: "applied" }, { status: "reached_review" }]).successRate).toBe(100);
  });
});
