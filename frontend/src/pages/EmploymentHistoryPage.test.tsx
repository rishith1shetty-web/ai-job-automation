import { describe, expect, it } from "vitest";
import { http, HttpResponse } from "msw";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderWithProviders } from "@/test/render";
import { BASE, server } from "@/test/server";
import { EmploymentHistoryPage } from "./EmploymentHistoryPage";

// A stored record whose display_order and confidence are BOTH 0 — the exact case where
// dashboard.py's Python `or` fallback diverges from a naive `??`.
const record = {
  id: 10,
  resume_id: 6493,
  display_order: 0,
  job_title: "Engineer",
  company_name: "Acme",
  city: "NYC",
  state: "NY",
  country: "USA",
  employment_type: "Full Time",
  start_month: "Jan",
  start_year: 2020,
  end_month: "Dec",
  end_year: 2022,
  currently_work_here: false,
  description: "",
  skills_used: ["Python"],
  technologies_used: ["Django"],
  confidence: 0,
  source: "Resume Experience",
};

async function openExistingRecord() {
  renderWithProviders(<EmploymentHistoryPage />);
  await screen.findByRole("option", { name: "Acme | Engineer" });
  const recordSelect = screen.getAllByRole("combobox")[0];
  await userEvent.selectOptions(recordSelect, "Acme | Engineer");
}

describe("EmploymentHistoryPage payload parity", () => {
  it("uses len(records) for display_order and 100 for confidence when stored values are 0 (Python `or`)", async () => {
    server.use(http.get(`${BASE}/employment-history`, () => HttpResponse.json([record])));
    let body: Record<string, unknown> | null = null;
    server.use(
      http.put(`${BASE}/employment-history/:id`, async ({ request, params }) => {
        body = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({ ...record, id: Number(params.id) });
      }),
    );

    await openExistingRecord();
    await userEvent.click(screen.getByRole("button", { name: "Save employment" }));

    await waitFor(() => expect(body).not.toBeNull());
    // recordsCount === 1, so `0 || 1` -> 1 ; `0 || 100` -> 100
    expect(body!.display_order).toBe(1);
    expect(body!.confidence).toBe(100);
  });

  it("issues PUT when a record is selected and POST for a new record", async () => {
    server.use(http.get(`${BASE}/employment-history`, () => HttpResponse.json([record])));
    const methods: string[] = [];
    server.use(
      http.put(`${BASE}/employment-history/:id`, async () => {
        methods.push("PUT");
        return HttpResponse.json(record);
      }),
      http.post(`${BASE}/employment-history`, async () => {
        methods.push("POST");
        return HttpResponse.json(record);
      }),
    );

    await openExistingRecord();
    await userEvent.click(screen.getByRole("button", { name: "Save employment" }));
    await waitFor(() => expect(methods).toContain("PUT"));
    expect(methods).not.toContain("POST");
  });

  it("clears end_month/end_year to null when 'Currently work here' is checked", async () => {
    server.use(http.get(`${BASE}/employment-history`, () => HttpResponse.json([record])));
    let body: Record<string, unknown> | null = null;
    server.use(
      http.put(`${BASE}/employment-history/:id`, async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(record);
      }),
    );

    await openExistingRecord();
    await userEvent.click(screen.getByLabelText("Currently work here"));
    await userEvent.click(screen.getByRole("button", { name: "Save employment" }));

    await waitFor(() => expect(body).not.toBeNull());
    expect(body!.currently_work_here).toBe(true);
    expect(body!.end_month).toBeNull();
    expect(body!.end_year).toBeNull();
  });
});
