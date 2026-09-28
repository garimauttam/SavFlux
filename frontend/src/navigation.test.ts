/**
 * navigation.test.ts — invariants on the destination list.
 *
 * `TABS` is read by the activity rail, the command palette and the `g`+key
 * handler, so a bad entry breaks three surfaces at once. These are the checks
 * that cannot be seen from a single component: ids and shortcut keys must be
 * unique, every shortcut must resolve back to its own destination, and the
 * list has to stay small enough to be a navigation rather than a filing
 * cabinet.
 */
import { describe, expect, it } from "vitest";
import { DEFAULT_TAB, LIBRARY_TABS, NAV_GROUPS, SHORTCUT_BY_KEY, TABS } from "./navigation";

describe("navigation — destination list", () => {
  it("has a unique id per destination", () => {
    const ids = TABS.map((t) => t.id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it("gives every destination a distinct one-character g-shortcut", () => {
    const keys = TABS.map((t) => t.shortcut);
    expect(keys.every((k) => k.length === 1)).toBe(true);
    expect(new Set(keys).size, `duplicate shortcut keys: ${keys.join(" ")}`).toBe(keys.length);
  });

  it("resolves every shortcut back to the destination that declares it", () => {
    expect(Object.keys(SHORTCUT_BY_KEY)).toHaveLength(TABS.length);
    for (const tab of TABS) {
      expect(SHORTCUT_BY_KEY[tab.shortcut]).toBe(tab.id);
    }
  });

  it("defaults to a destination that exists", () => {
    expect(TABS.map((t) => t.id)).toContain(DEFAULT_TAB);
  });

  /**
   * The number that caused the redesign.
   *
   * Seventeen tabs in a row did not fit a laptop, so the strip scrolled and
   * clipped its own tail. A vertical rail does not have that failure mode at
   * any window height, but a rail with twenty items is its own version of the
   * same problem, so the ceiling is pinned rather than left to drift.
   */
  it("stays short enough to be a rail and not a filing cabinet", () => {
    expect(TABS.length).toBeLessThanOrEqual(12);
  });

  it("opens on the agent, which is the product rather than one feature", () => {
    expect(DEFAULT_TAB).toBe("agent");
    expect(NAV_GROUPS[0].items[0].id).toBe("agent");
  });

  it("groups every destination, with no group left empty", () => {
    expect(NAV_GROUPS.length).toBeGreaterThan(1);
    for (const group of NAV_GROUPS) {
      expect(group.items.length, `${group.id} is empty`).toBeGreaterThan(0);
    }
    // Flattening the groups must reproduce TABS exactly — the rail renders the
    // groups, the palette renders TABS, and a gap between them is a
    // destination that exists in one surface and not the other.
    expect(NAV_GROUPS.flatMap((g) => g.items).map((t) => t.id)).toEqual(TABS.map((t) => t.id));
  });

  it("gives every destination a hint, because an icon alone is a guessing game", () => {
    for (const tab of TABS) {
      expect(tab.hint.length, `${tab.id} has no hint`).toBeGreaterThan(8);
    }
  });

  it("keeps the merged library collections addressable by id", () => {
    const libraryIds = LIBRARY_TABS.map((t) => t.id);
    expect(new Set(libraryIds).size).toBe(libraryIds.length);
    for (const former of ["prompts", "snippets", "activity", "history", "notifications", "bulk", "analytics", "slash"] as const) {
      expect(libraryIds, `${former} lost its Library sub-tab`).toContain(former);
    }
  });
});
