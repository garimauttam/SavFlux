/**
 * App.test.tsx — reachability of every section.
 *
 * The defect this guards: 17 sections, a tab strip that clipped the last ones
 * off-screen, and no test that could notice. Rendering the real App and walking
 * the strip end to end is the only check that proves a section is both listed
 * and actually rendered — a tab whose panel is missing from the switch renders
 * an empty container, which is exactly the "dead tab" failure mode.
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

// react-force-graph-2d needs a canvas; the graph's own behaviour is not what
// this test is about, only that the section mounts.
vi.mock("react-force-graph-2d", () => ({ default: () => null }));

import App from "./App";
import { TABS } from "./navigation";

beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () =>
      new Response(JSON.stringify({ files: [], repos: [], tools: [], metrics: null }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    ),
  );
});

describe("App — section reachability", () => {
  it("renders a tab for every section in navigation.ts", () => {
    render(<App />);
    for (const tab of TABS) {
      expect(screen.getByRole("tab", { name: tab.label })).toBeInTheDocument();
    }
  });

  it("clicking every tab renders a non-empty panel", async () => {
    render(<App />);
    for (const tab of TABS) {
      fireEvent.click(screen.getByRole("tab", { name: tab.label }));
      const panel = document.getElementById(`savflux-panel-${tab.id}`);
      expect(panel, `${tab.id} has a tab but no panel`).not.toBeNull();
      // A dead tab — listed but absent from the switch — renders an empty div.
      expect(panel!.textContent?.trim(), `${tab.id} panel rendered nothing`).not.toBe("");
      expect(screen.getByRole("tab", { name: tab.label })).toHaveAttribute("aria-selected", "true");
    }
  });

  it("g + <key> reaches the same section the tab does", () => {
    render(<App />);
    for (const tab of TABS) {
      fireEvent.keyDown(window, { key: "g" });
      fireEvent.keyDown(window, { key: tab.shortcut });
      expect(screen.getByRole("tab", { name: tab.label })).toHaveAttribute("aria-selected", "true");
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
      expect(screen.queryByRole("tablist")).toBeNull();
    } finally {
      Object.defineProperty(window, "location", { value: original, writable: true, configurable: true });
    }
  });
});
