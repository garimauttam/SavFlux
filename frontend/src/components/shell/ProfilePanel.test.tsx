import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ProfilePanel } from "./ProfilePanel";
import { AuthUserContext } from "../../lib/authUser";
const auth = vi.hoisted(() => ({ getSession: vi.fn(), updateUser: vi.fn() }));
vi.mock("../../lib/supabase", () => ({ supabase: { auth } }));
beforeEach(() => {
  vi.clearAllMocks();
  auth.getSession.mockResolvedValue({
    data: {
      session: {
        user: {
          id: "owner",
          email: "owner@example.com",
          user_metadata: { display_name: "Owner" },
        },
      },
    },
  });
  auth.updateUser.mockResolvedValue({ error: null });
});
function mount() {
  const connect = vi.fn();
  render(
    <AuthUserContext.Provider value="owner">
      <ProfilePanel
        github={{ connected: false, valid: false }}
        onConnectGitHub={connect}
        modelsContent={<p>Model settings content</p>}
        isDark
        onToggleTheme={vi.fn()}
      />
    </AuthUserContext.Provider>,
  );
  return connect;
}
describe("Profile & settings", () => {
  it("saves a display name only on explicit submit", async () => {
    mount();
    await waitFor(() =>
      expect(screen.getByLabelText("Display name")).toHaveValue("Owner"),
    );
    expect(auth.updateUser).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText("Display name"), {
      target: { value: " New name " },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save profile" }));
    await waitFor(() =>
      expect(auth.updateUser).toHaveBeenCalledWith({
        data: { display_name: "New name" },
      }),
    );
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Profile saved",
    );
  });
  it("makes GitHub, models and privacy reachable without enabling personalization", async () => {
    const connect = mount();
    await waitFor(() =>
      expect(screen.getByLabelText("Email")).toHaveValue("owner@example.com"),
    );
    fireEvent.click(screen.getByRole("tab", { name: "Connections" }));
    fireEvent.click(screen.getByRole("button", { name: "Connect GitHub" }));
    expect(connect).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByRole("tab", { name: "Models" }));
    expect(screen.getByText("Model settings content")).toBeVisible();
    fireEvent.click(screen.getByRole("tab", { name: "Privacy" }));
    expect(screen.getByText("Personalization is not enabled.")).toBeVisible();
    expect(screen.queryByRole("checkbox")).toBeNull();
  });
  it("shows save errors without claiming success", async () => {
    auth.updateUser.mockResolvedValueOnce({
      error: { message: "server unavailable" },
    });
    mount();
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Save profile" }),
      ).toBeEnabled(),
    );
    fireEvent.click(screen.getByRole("button", { name: "Save profile" }));
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Could not save",
    );
  });
});
