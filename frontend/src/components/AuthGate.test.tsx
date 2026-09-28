import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { AuthGate } from "./AuthGate";

beforeEach(() => {
  window.sessionStorage.clear();
  vi.restoreAllMocks();
});

describe("AuthGate", () => {
  it("asks for the single-instance owner key, then enters the workspace", async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      expect(new Headers(init?.headers).get("X-API-Key")).toBe("owner-secret");
      return new Response(JSON.stringify({ authenticated: true }), { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<AuthGate><div>Workspace ready</div></AuthGate>);

    expect(screen.getByRole("heading", { name: "Sign in to your instance" })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Instance owner key"), { target: { value: "owner-secret" } });
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));

    expect(await screen.findByText("Workspace ready")).toBeInTheDocument();
    expect(window.sessionStorage.getItem("savflux:ownerKey")).toBe("owner-secret");
  });

  it("does not retain a rejected key and explains the failure", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(
      JSON.stringify({ detail: "Unauthorized" }), { status: 401 },
    )));
    render(<AuthGate><div>Workspace ready</div></AuthGate>);

    fireEvent.change(screen.getByLabelText("Instance owner key"), { target: { value: "wrong" } });
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("That owner key was not accepted");
    expect(window.sessionStorage.getItem("savflux:ownerKey")).toBeNull();
    expect(screen.queryByText("Workspace ready")).toBeNull();
  });

  it("clears the browser session when sign-out is requested", async () => {
    window.sessionStorage.setItem("savflux:ownerKey", "owner-secret");
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 200 })));
    render(<AuthGate><div>Workspace ready</div></AuthGate>);
    expect(await screen.findByText("Workspace ready")).toBeInTheDocument();

    act(() => window.dispatchEvent(new Event("savflux:signout")));

    await waitFor(() => expect(screen.getByRole("heading", { name: "Sign in to your instance" })).toBeInTheDocument());
    expect(window.sessionStorage.getItem("savflux:ownerKey")).toBeNull();
  });
});
