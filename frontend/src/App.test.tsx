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
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";

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
  localStorage.clear();
  vi.clearAllMocks();
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
  it.each(["Agent", "Repositories", "Review"])("indexes a selected remote branch from %s", async (destination) => {
    const defaultFetch = globalThis.fetch;
    let indexed = false;
    const fetchSpy = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/branches?page=")) {
        return new Response(JSON.stringify({ branches: [{ name: "feature/remote", protected: false }], next_page: null }));
      }
      if (indexed && url.endsWith("/ingest/repos")) {
        return new Response(JSON.stringify({ repos: [{ repo_url: "https://github.com/owner/project", chunk_count: 1 }] }));
      }
      if (indexed && url.endsWith("/chat/indexed-files")) {
        return new Response(JSON.stringify({ files: [{ repo_url: "https://github.com/owner/project", file_name: "app.py", source: "app.py", language: "python" }] }));
      }
      if (url.endsWith("/ingest/github") && init?.method === "POST") {
        indexed = true;
        return new Response('data: {"step":"complete","status":"success"}\n\n', {
          headers: { "Content-Type": "text/event-stream" },
        });
      }
      return defaultFetch(input, init);
    });
    vi.stubGlobal("fetch", fetchSpy);
    render(<App />);
    fireEvent.click(await screen.findByRole("tab", { name: destination }));
    fireEvent.change(screen.getByLabelText("Public repository URL"), {
      target: { value: "https://github.com/owner/project" },
    });
    await screen.findByRole("option", { name: "feature/remote" });
    fireEvent.change(screen.getByLabelText("Remote branch"), { target: { value: "feature/remote" } });
    fireEvent.click(screen.getByRole("button", { name: "Index" }));
    await waitFor(() => {
      const call = fetchSpy.mock.calls.find(([url, init]) => String(url).endsWith("/ingest/github") && init?.method === "POST");
      expect(call).toBeDefined();
      expect(JSON.parse(String(call![1]!.body))).toEqual({ repo_url: "https://github.com/owner/project", branch: "feature/remote" });
    });
    expect(await screen.findByRole("button", { name: "Selected branch: feature/remote" })).toBeInTheDocument();
    expect(localStorage.getItem("savflux:test-user:branch:owner/project")).toBe("feature/remote");
    if (destination === "Repositories") {
      await waitFor(() => expect(screen.getByLabelText("Public repository URL")).toHaveValue(""));
      expect(screen.queryByLabelText("Remote branch")).not.toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Selected branch: feature/remote" })).toBeInTheDocument();
    }
  });

  it("keeps the active destination and repository draft on focus-style auth events", async () => {
    render(<App />);
    fireEvent.click(await screen.findByRole("tab", { name: "Repositories" }));
    const input = screen.getByLabelText("Public repository URL");
    fireEvent.change(input, { target: { value: "https://github.com/owner/project" } });
    const calls = supabaseAuth.onAuthStateChange.mock.calls as unknown as Array<[(event: string, session: unknown) => void]>;
    act(() => calls[calls.length - 1][0]("SIGNED_IN", {
      access_token: "app-test-token", user: { id: "test-user" },
    }));
    expect(screen.getByRole("tab", { name: "Repositories" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByLabelText("Public repository URL")).toBe(input);
    expect(input).toHaveValue("https://github.com/owner/project");
  });

  it("restores per-repository choices, loads public refs, and keeps a selection beyond page one", async () => {
    const firstRepo = "https://github.com/owner/project";
    const secondRepo = "https://github.com/owner/another";
    localStorage.setItem("savflux:test-user:activeRepoUrl", firstRepo);
    localStorage.setItem("savflux:test-user:branch:owner/project", "feature/later-page");
    localStorage.setItem("savflux:test-user:branch:owner/another", "release/2");
    const originalFetch = globalThis.fetch;
    const fetchSpy = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/ingest/repos")) return new Response(JSON.stringify({ repos: [
        { repo_url: firstRepo, chunk_count: 1 }, { repo_url: secondRepo, chunk_count: 1 },
      ] }));
      if (url.includes("/branches?page=1")) return new Response(JSON.stringify({
        branches: [{ name: "main" }], next_page: 2,
      }));
      if (url.includes("/branches?page=2")) return new Response(JSON.stringify({
        branches: [{ name: "feature/later-page" }, { name: "release/2" }], next_page: null,
      }));
      if (/github\/repos\/owner\/(project|another)$/.test(url)) return new Response('{"default_branch":"main"}');
      return originalFetch(input, init);
    });
    vi.stubGlobal("fetch", fetchSpy);
    const view = render(<App />);
    const branchButton = await screen.findByRole("button", { name: "Selected branch: feature/later-page" });
    fireEvent.click(branchButton);
    await screen.findByRole("option", { name: "feature/later-page" });
    expect(fetchSpy.mock.calls.some(([url]) => String(url).endsWith("project/branches?page=2"))).toBe(true);
    fireEvent.click(branchButton); // close
    fireEvent.click(screen.getByRole("button", { name: /owner\/project/ }));
    fireEvent.click(screen.getByRole("option", { name: /owner\/another/ }));
    expect(await screen.findByRole("button", { name: "Selected branch: release/2" })).toBeInTheDocument();
    view.unmount();
    render(<App />);
    expect(await screen.findByRole("button", { name: "Selected branch: release/2" })).toBeInTheDocument();
  });

  it("does not reset a saved branch when public branch lookup fails", async () => {
    localStorage.setItem("savflux:test-user:activeRepoUrl", "https://github.com/owner/project");
    localStorage.setItem("savflux:test-user:branch:owner/project", "feature/offline");
    const originalFetch = globalThis.fetch;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/ingest/repos")) return new Response(JSON.stringify({ repos: [{ repo_url: "https://github.com/owner/project", chunk_count: 1 }] }));
      if (url.includes("/github/repos/")) return new Response('{"detail":"rate limited"}', { status: 429 });
      return originalFetch(input, init);
    }));
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Selected branch: feature/offline" }));
    await screen.findByText(/Remote branches are unavailable/);
    expect(screen.getByRole("button", { name: "Selected branch: feature/offline" })).toBeInTheDocument();
  });

  it("does not save a branch or announce success when repository ingestion fails", async () => {
    const originalFetch = globalThis.fetch;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/branches?page=")) return new Response('{"branches":[{"name":"feature/fail"}]}');
      if (url.endsWith("/ingest/github")) return new Response('data: {"step":"complete","status":"error","message":"Clone failed"}\n\n');
      return originalFetch(input, init);
    }));
    render(<App />);
    fireEvent.click(await screen.findByRole("tab", { name: "Repositories" }));
    fireEvent.change(screen.getByLabelText("Public repository URL"), { target: { value: "https://github.com/owner/project" } });
    await screen.findByRole("option", { name: "feature/fail" });
    fireEvent.change(screen.getByLabelText("Remote branch"), { target: { value: "feature/fail" } });
    fireEvent.click(screen.getByRole("button", { name: "Index" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Clone failed");
    expect(screen.getByLabelText("Public repository URL")).toHaveValue("https://github.com/owner/project");
    expect(screen.getByLabelText("Remote branch")).toHaveValue("feature/fail");
    expect(localStorage.getItem("savflux:test-user:branch:owner/project")).toBeNull();
    expect(screen.queryByRole("button", { name: "Selected branch: feature/fail" })).not.toBeInTheDocument();
  });

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
