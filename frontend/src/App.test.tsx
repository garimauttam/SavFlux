/**
 * App.test.tsx — reachability of every destination.
 *
 * The defect this guards is the one this redesign fixed: 17 sections in a tab
 * strip that clipped its own tail, with no test able to notice. Rendering the
 * real App and walking the rail end to end is the only check that proves a
 * destination is both listed and actually rendered — a destination whose panel
 * is missing from the switch renders an empty container, which is exactly the
 * "dead tab" failure mode, and a "dead rail item" is the same failure with a
 * nicer icon.
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

// Auth is mocked; route reachability tests do not need a real Supabase project.
const { supabaseAuth } = vi.hoisted(() => ({
  supabaseAuth: {
    getSession: vi.fn(),
    onAuthStateChange: vi.fn(() => ({ data: { subscription: { unsubscribe: vi.fn() } } })),
    signOut: vi.fn(),
  },
}));
vi.mock("./lib/supabase", () => ({ supabase: { auth: supabaseAuth }, supabaseAuthConfigured: true }));

// react-force-graph-2d needs a canvas; the graph's own behaviour is not what
// this test is about, only that the destination mounts.
vi.mock("react-force-graph-2d", () => ({ default: () => null }));

import App from "./App";
import { TABS } from "./navigation";

beforeEach(() => {
  supabaseAuth.getSession.mockResolvedValue({
    data: { session: { access_token: "app-test-token", user: { id: "test-user" } } },
    error: null,
  });
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      // The shell's two new integrations are queried on mount; answering them
      // with the empty-object shape keeps every panel out of its error state,
      // so a failure below is about reachability and not about a 404.
      if (url.includes("/api/v1/github/status")) {
        return new Response(
          JSON.stringify({ connected: false, valid: false, source: null, user: null, message: null }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      if (url.includes("/api/v1/models")) {
        return new Response(
          JSON.stringify({
            provider: "ollama",
            provider_label: "Local · Ollama",
            free: true,
            base_url: "http://localhost:11434",
            available: false,
            reachable: false,
            kind: "not_running",
            hint: "Ollama is not reachable.",
            chat_model: "qwen2.5-coder:7b",
            review_model: "qwen2.5-coder:7b",
            embedding_model: "all-MiniLM-L6-v2",
            embedding_local: true,
            models: [],
            suggested: [],
            selection_source: "env",
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      return new Response(
        JSON.stringify({ files: [], repos: [], tools: [], metrics: null, branches: [], pulls: [] }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    }),
  );
});

describe("App — destination reachability", () => {
  it("renders a rail item for every destination in navigation.ts", async () => {
    render(<App />);
    await waitFor(() => expect(screen.getByRole("tab", { name: TABS[0].label })).toBeInTheDocument());
    for (const tab of TABS) {
      expect(screen.getByRole("tab", { name: tab.label })).toBeInTheDocument();
    }
  });

  it("clicking every destination renders a non-empty panel", async () => {
    render(<App />);
    await waitFor(() => expect(screen.getByRole("tab", { name: TABS[0].label })).toBeInTheDocument());
    for (const tab of TABS) {
      fireEvent.click(screen.getByRole("tab", { name: tab.label }));
      const panel = document.getElementById(`savflux-panel-${tab.id}`);
      expect(panel, `${tab.id} has a rail item but no panel`).not.toBeNull();
      // The dependency graph is loaded on demand, so its container holds the
      // Suspense fallback for a tick — text that would satisfy an emptiness
      // check without the panel ever mounting.
      if (tab.id === "graph") {
        await waitFor(() => expect(panel!.textContent).toContain("No repo indexed yet"), {
          timeout: 4000,
        });
      }
      expect(panel!.textContent?.trim(), `${tab.id} panel rendered nothing`).not.toBe("");
      expect(screen.getByRole("tab", { name: tab.label })).toHaveAttribute("aria-selected", "true");
    }
  });

  it("g + <key> reaches the same destination the rail does", async () => {
    render(<App />);
    await waitFor(() => expect(screen.getByRole("tab", { name: TABS[0].label })).toBeInTheDocument());
    for (const tab of TABS) {
      fireEvent.keyDown(window, { key: "g" });
      fireEvent.keyDown(window, { key: tab.shortcut });
      expect(screen.getByRole("tab", { name: tab.label })).toHaveAttribute("aria-selected", "true");
    }
  });

  /**
   * The merge that made nine destinations possible.
   *
   * Eight of the old tabs — prompts, snippets, history, activity, inbox,
   * commands, bulk, metrics — were small local collections, each with a tab.
   * They are still there, so a test has to prove they did not get dropped in
   * the consolidation: "reachable somewhere" and "still reachable" are
   * different claims.
   */
  it("still reaches every merged collection through the Library", async () => {
    render(<App />);
    await waitFor(() => expect(screen.getByRole("tab", { name: TABS[0].label })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("tab", { name: "Library" }));
    for (const label of [
      "Prompts",
      "Snippets",
      "History",
      "Activity",
      "Inbox",
      "Commands",
      "Bulk",
      "Metrics",
    ]) {
      const sub = screen.getByRole("tab", { name: label });
      fireEvent.click(sub);
      const panel = document.getElementById(`savflux-library-${libraryIdFor(label)}`);
      expect(panel, `${label} has no panel inside the Library`).not.toBeNull();
    }
  });

  it("shows the share view on /s/ without running the workspace's hooks", () => {
    const original = window.location;
    Object.defineProperty(window, "location", {
      value: { ...original, pathname: "/s/abc123" },
      writable: true,
      configurable: true,
    });
    try {
      render(<App />);
      expect(screen.queryByRole("tab")).toBeNull();
    } finally {
      Object.defineProperty(window, "location", { value: original, writable: true, configurable: true });
    }
  });
});

function libraryIdFor(label: string): string {
  const map: Record<string, string> = {
    Prompts: "prompts",
    Snippets: "snippets",
    History: "history",
    Activity: "activity",
    Inbox: "notifications",
    Commands: "slash",
    Bulk: "bulk",
    Metrics: "analytics",
  };
  return map[label];
}
