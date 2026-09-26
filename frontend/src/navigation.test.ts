/**
 * navigation.test.ts — invariants on the section list.
 *
 * TABS is now read by the tab strip, the `g`+key handler and the command
 * palette, so a bad entry breaks three surfaces at once. The checks here are
 * the ones that cannot be seen from a single component: ids and shortcut keys
 * must be unique (a duplicated key silently sends two sections to the same
 * letter), and every shortcut must resolve back to its own section.
 */
import { describe, expect, it } from "vitest";
import { DEFAULT_TAB, SHORTCUT_BY_KEY, TABS } from "./navigation";

describe("navigation — section list", () => {
  it("has a unique id per section", () => {
    const ids = TABS.map((t) => t.id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it("gives every section a distinct one-character g-shortcut", () => {
    const keys = TABS.map((t) => t.shortcut);
    expect(keys.every((k) => k.length === 1)).toBe(true);
    expect(new Set(keys).size, `duplicate shortcut keys: ${keys.join(" ")}`).toBe(keys.length);
  });

  it("resolves every shortcut back to the section that declares it", () => {
    expect(Object.keys(SHORTCUT_BY_KEY)).toHaveLength(TABS.length);
    for (const tab of TABS) {
      expect(SHORTCUT_BY_KEY[tab.shortcut]).toBe(tab.id);
    }
  });

  it("defaults to a section that exists", () => {
    expect(TABS.map((t) => t.id)).toContain(DEFAULT_TAB);
  });

  it("puts the three work surfaces first, so they are never the clipped tail", () => {
    expect(TABS.slice(0, 4).map((t) => t.id)).toEqual(["chat", "agent", "review", "write"]);
  });
});
