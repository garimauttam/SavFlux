/**
 * TabBar.test.tsx — the section strip.
 *
 * These tests exist because of a concrete defect, not as coverage padding: the
 * app has 17 sections, the sidebar takes 288px, and the old strip was a `flex`
 * row with no overflow handling, so on a laptop everything past "Prompts" was
 * clipped with no scrollbar, no arrows and no way to click it. The tests below
 * pin the three properties that make every section reachable again — all tabs
 * are rendered as tabs, the strip can be scrolled in both directions when it
 * overflows, and selection from elsewhere reveals the tab that was selected.
 */
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { TabBar } from "./TabBar";
import { TABS, type Tab } from "../navigation";

/** jsdom has no layout engine, so overflow is whatever the test declares. */
function setLayout(el: HTMLElement, layout: { scrollWidth: number; clientWidth: number; scrollLeft?: number }) {
  Object.defineProperty(el, "scrollWidth", { value: layout.scrollWidth, configurable: true });
  Object.defineProperty(el, "clientWidth", { value: layout.clientWidth, configurable: true });
  Object.defineProperty(el, "scrollLeft", { value: layout.scrollLeft ?? 0, configurable: true, writable: true });
}

function tablist() {
  return screen.getByRole("tablist");
}

describe("TabBar — every section is a reachable tab", () => {
  it("renders one tab per section, in TABS order", () => {
    render(<TabBar activeTab="chat" onSelect={() => {}} />);
    const tabs = screen.getAllByRole("tab");
    expect(tabs).toHaveLength(TABS.length);
    expect(tabs.map((t) => t.textContent)).toEqual(TABS.map((t) => t.label));
  });

  it("marks exactly the active tab as selected, and points it at its panel", () => {
    render(<TabBar activeTab="review" onSelect={() => {}} />);
    const selected = screen.getAllByRole("tab").filter((t) => t.getAttribute("aria-selected") === "true");
    expect(selected).toHaveLength(1);
    expect(selected[0]).toHaveAccessibleName("Code Review");
    expect(selected[0]).toHaveAttribute("aria-controls", "savflux-panel-review");
  });

  it("uses a roving tabindex so the strip is one tab stop", () => {
    render(<TabBar activeTab="graph" onSelect={() => {}} />);
    const tabindexes = screen.getAllByRole("tab").map((t) => t.getAttribute("tabindex"));
    expect(tabindexes.filter((t) => t === "0")).toHaveLength(1);
    expect(tabindexes.filter((t) => t === "-1")).toHaveLength(TABS.length - 1);
    expect(screen.getByRole("tab", { name: "Dep. Graph" })).toHaveAttribute("tabindex", "0");
  });

  it("selects a tab when it is clicked", () => {
    const onSelect = vi.fn();
    render(<TabBar activeTab="chat" onSelect={onSelect} />);
    fireEvent.click(screen.getByRole("tab", { name: "Health" }));
    expect(onSelect).toHaveBeenCalledWith("health");
  });

  it("announces the g-shortcut for each tab, so the shortcuts are discoverable", () => {
    render(<TabBar activeTab="chat" onSelect={() => {}} />);
    for (const def of TABS) {
      expect(screen.getByRole("tab", { name: def.label })).toHaveAttribute(
        "title",
        `${def.label} — g ${def.shortcut}`,
      );
    }
  });
});

describe("TabBar — keyboard navigation", () => {
  const cases: [string, Tab, Tab][] = [
    ["ArrowRight", "chat", "agent"],
    ["ArrowLeft", "chat", "history"], // wraps backwards instead of dead-ending
    ["Home", "history", "chat"],
    ["End", "chat", "history"],
  ];

  it.each(cases)("%s moves selection from %s to %s", (key, from, to) => {
    const onSelect = vi.fn();
    render(<TabBar activeTab={from} onSelect={onSelect} />);
    fireEvent.keyDown(tablist(), { key });
    expect(onSelect).toHaveBeenCalledWith(to);
  });

  it("moves focus with the selection, so the arrow keys keep working", () => {
    render(<TabBar activeTab="chat" onSelect={() => {}} />);
    fireEvent.keyDown(tablist(), { key: "ArrowRight" });
    expect(document.activeElement).toBe(screen.getByRole("tab", { name: "Agent" }));
  });

  it("leaves non-navigation keys alone", () => {
    const onSelect = vi.fn();
    render(<TabBar activeTab="chat" onSelect={onSelect} />);
    fireEvent.keyDown(tablist(), { key: "x" });
    expect(onSelect).not.toHaveBeenCalled();
  });
});

describe("TabBar — overflow scrolling", () => {
  it("hides the arrows when the whole strip fits", () => {
    render(<TabBar activeTab="chat" onSelect={() => {}} />);
    setLayout(tablist(), { scrollWidth: 800, clientWidth: 1200 });
    fireEvent.scroll(tablist());
    expect(screen.queryByTestId("tabbar-scroll-left")).toBeNull();
    expect(screen.queryByTestId("tabbar-scroll-right")).toBeNull();
  });

  it("offers a way forward when the tail is off-screen", () => {
    render(<TabBar activeTab="chat" onSelect={() => {}} />);
    setLayout(tablist(), { scrollWidth: 1872, clientWidth: 992, scrollLeft: 0 });
    fireEvent.scroll(tablist());
    expect(screen.queryByTestId("tabbar-scroll-left")).toBeNull();
    const right = screen.getByTestId("tabbar-scroll-right");
    fireEvent.click(right);
    expect(vi.mocked(Element.prototype.scrollBy)).toHaveBeenCalledWith({ left: 240, behavior: "smooth" });
  });

  it("offers a way back once scrolled, and scrolls backwards", () => {
    render(<TabBar activeTab="prompts" onSelect={() => {}} />);
    // 880px of overflow, 400px scrolled: both directions are still available.
    setLayout(tablist(), { scrollWidth: 1872, clientWidth: 992, scrollLeft: 400 });
    fireEvent.scroll(tablist());
    const left = screen.getByTestId("tabbar-scroll-left");
    expect(screen.getByTestId("tabbar-scroll-right")).toBeInTheDocument();
    fireEvent.click(left);
    expect(vi.mocked(Element.prototype.scrollBy)).toHaveBeenCalledWith({ left: -240, behavior: "smooth" });
  });

  it("drops the forward arrow at the end of the strip", () => {
    render(<TabBar activeTab="history" onSelect={() => {}} />);
    // scrollLeft 880 == scrollWidth - clientWidth, i.e. nothing left to the right.
    setLayout(tablist(), { scrollWidth: 1872, clientWidth: 992, scrollLeft: 880 });
    fireEvent.scroll(tablist());
    expect(screen.queryByTestId("tabbar-scroll-right")).toBeNull();
  });
});

describe("TabBar — revealing the selected tab", () => {
  it("scrolls a tab selected from elsewhere into view", () => {
    const { rerender } = render(<TabBar activeTab="chat" onSelect={() => {}} />);
    const spy = vi.spyOn(Element.prototype, "scrollIntoView").mockImplementation(() => {});
    rerender(<TabBar activeTab="history" onSelect={() => {}} />);
    expect(spy).toHaveBeenCalled();
    // The element that was scrolled must be the newly selected tab, not any tab.
    const instances = spy.mock.instances;
    const target = instances[instances.length - 1];
    expect(target).toBe(screen.getByRole("tab", { name: "History" }));
    expect(spy).toHaveBeenLastCalledWith({ block: "nearest", inline: "nearest" });
  });
});
