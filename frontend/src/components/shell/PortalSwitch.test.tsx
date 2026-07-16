import { http, HttpResponse } from "msw";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";

import { renderWithProviders } from "@/test/render";
import { BASE, server } from "@/test/server";
import { Sidebar } from "./Sidebar";

/**
 * Switching portals must reach the Connect button.
 *
 * The Sidebar renders the LinkedIn OR the Dice connect panel from
 * `status.active_portal ?? "linkedin"`. A fresh session reports active_portal: null, so it
 * starts on the LinkedIn panel — acceptable. But once the user explicitly switches, the
 * refetched GET /portal/active must report the new portal, otherwise the Sidebar falls back
 * to LinkedIn forever and the Dice Connect button can never be rendered (the regression that
 * made POST /dice/connect unreachable).
 *
 * These tests model the real backend contract: POST /portal/active/{portal} changes what the
 * subsequent GET returns.
 */
function useStatefulPortal(initial: string | null = null) {
  let active: string | null = initial;
  server.use(
    http.get(`${BASE}/portal/active`, () =>
      HttpResponse.json({
        active_portal: active,
        linkedin_connected: false,
        dice_connected: false,
      }),
    ),
    http.post(`${BASE}/portal/active/:portal`, ({ params }) => {
      active = params.portal as string;
      return HttpResponse.json({ active_portal: active, status: "active" });
    }),
  );
}

describe("portal switch — the Connect button is reachable", () => {
  beforeEach(() => useStatefulPortal(null));

  it("renders Dice Connect after switching to Dice", async () => {
    const user = userEvent.setup();
    renderWithProviders(<Sidebar />);

    // Fresh session: starts on the LinkedIn panel, no Dice Connect button.
    expect(await screen.findByRole("button", { name: /Connect LinkedIn/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Connect Dice/i })).toBeNull();

    // Explicitly select Dice, then switch.
    await user.click(screen.getByRole("button", { name: "Dice" }));
    await user.click(await screen.findByRole("button", { name: /Switch to Dice/i }));

    // After the refetch the Dice panel renders and Connect Dice is available + enabled.
    const connectDice = await screen.findByRole("button", { name: /Connect Dice/i });
    expect(connectDice).toBeEnabled();
    expect(screen.queryByRole("button", { name: /Connect LinkedIn/i })).toBeNull();
  });

  it("renders LinkedIn Connect after switching back", async () => {
    const user = userEvent.setup();
    renderWithProviders(<Sidebar />);

    await user.click(screen.getByRole("button", { name: "Dice" }));
    await user.click(await screen.findByRole("button", { name: /Switch to Dice/i }));
    await screen.findByRole("button", { name: /Connect Dice/i });

    await user.click(screen.getByRole("button", { name: "LinkedIn" }));
    await user.click(await screen.findByRole("button", { name: /Switch to Linkedin/i }));

    expect(await screen.findByRole("button", { name: /Connect LinkedIn/i })).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("button", { name: /Connect Dice/i })).toBeNull());
  });

  it("does not show the portal as connected merely because it was selected", async () => {
    const user = userEvent.setup();
    renderWithProviders(<Sidebar />);

    await user.click(screen.getByRole("button", { name: "Dice" }));
    await user.click(await screen.findByRole("button", { name: /Switch to Dice/i }));

    await screen.findByRole("button", { name: /Connect Dice/i });
    expect(screen.getByText(/Dice not connected/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Disconnect Dice/i })).toBeNull();
  });
});
