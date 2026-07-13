import { describe, expect, it } from "vitest";
import { http, HttpResponse } from "msw";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderWithProviders } from "@/test/render";
import { BASE, server } from "@/test/server";
import { ScreeningKnowledgePage } from "./ScreeningKnowledgePage";

describe("ScreeningKnowledgePage answer payload parity", () => {
  it("serializes empty currency to null, wraps evidence as { note }, and stamps source/resume_id", async () => {
    let body: Record<string, unknown> | null = null;
    server.use(
      http.post(`${BASE}/screening/knowledge`, async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({ id: 1 });
      }),
    );

    const { queryClient } = renderWithProviders(<ScreeningKnowledgePage />);
    // Ensure the resume query resolved so resume_id is populated (not null).
    await waitFor(() => expect(queryClient.getQueryData(["resume", "latest"])).toBeTruthy());

    await userEvent.type(screen.getByPlaceholderText("Example: What is your expected CTC?"), "Expected CTC?");
    await userEvent.click(screen.getByRole("button", { name: "Save answer" }));

    await waitFor(() => expect(body).not.toBeNull());
    expect(body!.currency).toBeNull(); // default "" -> null (dashboard.py `currency or None`)
    expect(body!.evidence).toEqual({ note: "" });
    expect(body!.source).toBe("Manual");
    expect(body!.resume_id).toBe(6493);
    expect(body!.question_text).toBe("Expected CTC?");
  });

  it("keeps a non-empty selected currency verbatim", async () => {
    let body: Record<string, unknown> | null = null;
    server.use(
      http.post(`${BASE}/screening/knowledge`, async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({ id: 2 });
      }),
    );

    renderWithProviders(<ScreeningKnowledgePage />);
    const currency = await screen.findByDisplayValue("—"); // "" option renders as "—"
    await userEvent.selectOptions(currency, "USD");
    await userEvent.click(screen.getByRole("button", { name: "Save answer" }));

    await waitFor(() => expect(body).not.toBeNull());
    expect(body!.currency).toBe("USD");
  });
});
