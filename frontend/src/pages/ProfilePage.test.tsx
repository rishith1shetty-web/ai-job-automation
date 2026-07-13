import { describe, expect, it } from "vitest";
import { http, HttpResponse } from "msw";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderWithProviders } from "@/test/render";
import { BASE, fixtures, server } from "@/test/server";
import { ProfilePage } from "./ProfilePage";

const EXPECTED_KEYS = [
  "first_name", "last_name", "email", "mobile_phone", "phone_country_code",
  "current_location", "linkedin_url", "portfolio_url", "github_url",
  "current_job_title", "current_company", "professional_headline",
].sort();

describe("ProfilePage candidate-profile payload parity", () => {
  it("PUTs exactly the 12 dashboard.py keys with NO portal field", async () => {
    let body: Record<string, unknown> | null = null;
    server.use(
      http.put(`${BASE}/candidate-profile`, async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(fixtures.candidateProfile);
      }),
    );

    renderWithProviders(<ProfilePage />);
    const save = await screen.findByRole("button", { name: "Save candidate profile" });
    await userEvent.click(save);

    await waitFor(() => expect(body).not.toBeNull());
    expect(Object.keys(body!).sort()).toEqual(EXPECTED_KEYS);
    expect(body!).not.toHaveProperty("portal");
  });
});
