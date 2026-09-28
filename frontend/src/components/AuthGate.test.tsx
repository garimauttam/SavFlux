import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AuthGate } from "./AuthGate";

const { auth, supabaseMock } = vi.hoisted(() => {
  const auth = {
    getSession: vi.fn(),
    onAuthStateChange: vi.fn(),
    signInWithOAuth: vi.fn(),
    signInWithPassword: vi.fn(),
    signUp: vi.fn(),
    resetPasswordForEmail: vi.fn(),
    updateUser: vi.fn(),
    signOut: vi.fn(),
  };
  return { auth, supabaseMock: { auth } };
});

vi.mock("../lib/supabase", () => ({ supabase: supabaseMock, supabaseAuthConfigured: true }));
vi.mock("../api", () => ({ apiUrl: (path: string) => path }));

const session = { access_token: "supabase-access-token", user: { id: "user-1", email: "person@example.com" } };

beforeEach(() => {
  auth.getSession.mockResolvedValue({ data: { session: null }, error: null });
  auth.onAuthStateChange.mockReturnValue({ data: { subscription: { unsubscribe: vi.fn() } } });
  auth.signInWithOAuth.mockResolvedValue({ data: {}, error: null });
  auth.signInWithPassword.mockResolvedValue({ data: { session }, error: null });
  auth.signUp.mockResolvedValue({ data: { session: null }, error: null });
  auth.resetPasswordForEmail.mockResolvedValue({ data: {}, error: null });
  auth.updateUser.mockResolvedValue({ data: { user: session.user }, error: null });
  auth.signOut.mockResolvedValue({ error: null });
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ authenticated: true }), { status: 200 })));
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe("AuthGate", () => {
  it("offers Google and email/password sign-in", async () => {
    render(<AuthGate><div>private workspace</div></AuthGate>);
    expect(await screen.findByRole("button", { name: /Continue with Google/ })).toBeInTheDocument();
    expect(screen.getByLabelText("Email address")).toBeInTheDocument();
    expect(screen.getByLabelText("Password")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Continue with Google/ }));
    await waitFor(() => expect(auth.signInWithOAuth).toHaveBeenCalledWith({
      provider: "google",
      options: { redirectTo: window.location.origin },
    }));
  });

  it("validates a Supabase email session with the SavFlux API before opening the workspace", async () => {
    render(<AuthGate><div>private workspace</div></AuthGate>);
    await screen.findByRole("button", { name: "Sign in" });
    fireEvent.change(screen.getByLabelText("Email address"), { target: { value: "person@example.com" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "long-enough-password" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByText("private workspace")).toBeInTheDocument();
    expect(auth.signInWithPassword).toHaveBeenCalledWith({ email: "person@example.com", password: "long-enough-password" });
    expect(fetch).toHaveBeenCalledWith("/api/v1/auth/session", {
      headers: { Authorization: "Bearer supabase-access-token" },
    });
  });

  it("supports account creation and confirmation messaging", async () => {
    render(<AuthGate><div>private workspace</div></AuthGate>);
    await screen.findByRole("button", { name: /Create an account/ });
    fireEvent.click(screen.getByRole("button", { name: /Create an account/ }));
    fireEvent.change(screen.getByLabelText("Email address"), { target: { value: "new@example.com" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "long-enough-password" } });
    fireEvent.click(screen.getByRole("button", { name: "Create account" }));

    expect(await screen.findByRole("status")).toHaveTextContent("Check your email to verify your account");
    expect(auth.signUp).toHaveBeenCalledWith({
      email: "new@example.com",
      password: "long-enough-password",
      options: { emailRedirectTo: window.location.origin },
    });
  });

  it("sends password reset instructions without confirming whether an account exists", async () => {
    render(<AuthGate><div>private workspace</div></AuthGate>);
    await screen.findByRole("button", { name: /Forgot password/ });
    fireEvent.click(screen.getByRole("button", { name: /Forgot password/ }));
    fireEvent.change(screen.getByLabelText("Email address"), { target: { value: "person@example.com" } });
    fireEvent.click(screen.getByRole("button", { name: "Send reset link" }));
    expect(await screen.findByRole("status")).toHaveTextContent("If an account exists");
  });

  it("handles Supabase recovery links and lets the user set a new password", async () => {
    render(<AuthGate><div>private workspace</div></AuthGate>);
    await screen.findByRole("button", { name: /Forgot password/ });
    const onAuthStateChange = auth.onAuthStateChange.mock.calls[0][0];
    act(() => onAuthStateChange("PASSWORD_RECOVERY", session));

    expect(await screen.findByLabelText("New password")).toBeInTheDocument();
    expect(screen.queryByLabelText("Email address")).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("New password"), { target: { value: "a-new-secure-password" } });
    fireEvent.click(screen.getByRole("button", { name: "Update password" }));

    await waitFor(() => expect(auth.updateUser).toHaveBeenCalledWith({ password: "a-new-secure-password" }));
  });
});
