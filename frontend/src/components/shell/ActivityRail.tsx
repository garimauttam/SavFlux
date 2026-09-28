/**
 * ActivityRail.tsx — the left navigation column.
 *
 * WHY A RAIL AND NOT A TAB STRIP
 * ------------------------------
 * Seventeen tabs in a row did not fit a laptop, scrolled behind arrows, and
 * made the sections that matter look arbitrary. Nine destinations in a vertical
 * rail fit every screen without scrolling, and grouping them by what you are
 * doing (Work / Understand / Ship / Keep) means the two you want are usually
 * adjacent instead of seventeen clicks apart.
 *
 * The rail expands to show labels on hover-park or by pin, because icon-only
 * navigation is a guessing game for a product with a first-run user: nobody
 * knows that the pink bot icon is the agent. The labels are always in the DOM
 * (`sr-only` when collapsed) so the destination stays reachable by screen
 * reader and by test, and the tooltip is a convenience rather than the only
 * way to know.
 */

import { useEffect, useRef, useState } from "react";
import { ChevronsLeft, ChevronsRight, Github, Plus, Settings2 } from "lucide-react";
import { NAV_GROUPS, type Tab } from "../../navigation";

interface ActivityRailProps {
  active: Tab;
  onSelect: (tab: Tab) => void;
  /** Set false when there is nothing indexed yet and browsing would be a dead end. */
  hasIndex: boolean;
  githubConnected: boolean;
  onConnectGitHub: () => void;
  onOpenSettings: () => void;
}

export function ActivityRail({
  active,
  onSelect,
  hasIndex,
  githubConnected,
  onConnectGitHub,
  onOpenSettings,
}: ActivityRailProps) {
  // Pinned, not merely hovered.
  //
  // The first version of this rail expanded on hover and collapsed on leave,
  // which sounds polite and is actually hostile: reaching for the composer or
  // a citation — the two things you do constantly — collapsed the labels out
  // from under you, so the rail changed width every few seconds. Pinning means
  // the navigation stays put, and hovering only matters when it is *pinned
  // closed*, which is the one case where the reader has asked for less.
  //
  // The pinned state is remembered, and defaults to open on a screen wide
  // enough for the labels: nine unfamiliar glyphs with no text is a guessing
  // game for anyone arriving for the first time.
  const [pinned, setPinned] = useState(() => {
    try {
      const stored = localStorage.getItem("savflux:railPinned");
      if (stored === "1") return true;
      if (stored === "0") return false;
    } catch {
      /* private mode — fall through to the width default */
    }
    try {
      return typeof window !== "undefined" && window.innerWidth >= 1024;
    } catch {
      return true;
    }
  });
  const [hovered, setHovered] = useState(false);
  const railRef = useRef<HTMLDivElement>(null);

  // Hover is a desktop affordance. On a touch device there is no hover, so the
  // pinned state is the only thing that opens the labels; the `title`
  // attributes carry the names either way.
  const isTouch = typeof window !== "undefined" && window.matchMedia?.("(pointer: coarse)")?.matches;
  const open = !isTouch && (pinned || hovered);

  useEffect(() => {
    try {
      localStorage.setItem("savflux:railPinned", pinned ? "1" : "0");
    } catch {
      /* private mode — the preference just does not persist */
    }
  }, [pinned]);

  useEffect(() => {
    const el = railRef.current;
    if (!el) return;
    const enter = () => setHovered(true);
    const leave = () => setHovered(false);
    el.addEventListener("mouseenter", enter);
    el.addEventListener("mouseleave", leave);
    return () => {
      el.removeEventListener("mouseenter", enter);
      el.removeEventListener("mouseleave", leave);
    };
  }, []);

  return (
    <div
      ref={railRef}
      data-testid="activity-rail"
      data-expanded={open ? "true" : "false"}
      className="sf-surface flex shrink-0 flex-col border-r sf-line transition-[width] duration-150 ease-out"
      style={{ width: open ? "var(--sf-rail-open)" : "var(--sf-rail)" }}
    >
      {/* Wordmark — the product name, not a tab. */}
      <div className="flex h-12 items-center gap-2 border-b sf-line px-3">
        <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-indigo-500/15 text-[13px] font-bold text-indigo-300">
          S
        </div>
        {open && (
          <span className="sf-text truncate text-[13px] font-semibold tracking-tight">SavFlux</span>
        )}
      </div>

      <nav
        aria-label="Sections"
        className="scrollbar-none flex-1 overflow-y-auto overflow-x-hidden py-2"
      >
        {NAV_GROUPS.map((group) => (
          <div key={group.id} className="mb-1.5 px-2">
            {open && (
              <div className="sf-mute px-2 pb-1 pt-2 text-[10px] font-semibold uppercase tracking-[0.08em]">
                {group.label}
              </div>
            )}
            {open && <div className="mx-2 mb-1 h-px sf-line" />}
            <ul className="space-y-0.5">
              {group.items.map((item) => {
                const selected = item.id === active;
                const Icon = item.Icon;
                return (
                  <li key={item.id}>
                    <button
                      type="button"
                      role="tab"
                      aria-selected={selected}
                      aria-label={item.label}
                      title={open ? undefined : `${item.label} — ${item.hint}`}
                      onClick={() => onSelect(item.id)}
                      className={[
                        "group relative flex w-full items-center rounded-lg text-left transition-colors",
                        "h-9 text-[13px]",
                        open ? "gap-2.5 px-2" : "justify-center px-0",
                        selected
                          ? "sf-accent-soft text-[var(--sf-accent)]"
                          : "text-[var(--sf-text-dim)] hover:bg-[var(--sf-raised)] hover:text-[var(--sf-text)]",
                      ].join(" ")}
                    >
                      {selected && (
                        <span
                          aria-hidden
                          className="absolute left-0 top-1/2 h-4 w-[2.5px] -translate-y-1/2 rounded-r-full"
                          style={{ background: "var(--sf-accent)" }}
                        />
                      )}
                      <Icon className="h-[15px] w-[15px] shrink-0" />
                      {open && <span className="truncate">{item.label}</span>}
                    </button>
                  </li>
                );
              })}
            </ul>
          </div>
        ))}

        {/* Repositories needs an account before it can show anything useful, so
            an unconnected state gets a button rather than an empty panel. */}
        {!githubConnected && (
          <div className="px-2 pt-2">
            <button
              type="button"
              onClick={onConnectGitHub}
              title={open ? undefined : "Connect GitHub"}
              className={[
                "flex h-9 w-full items-center rounded-lg border border-dashed sf-line text-[13px] transition-colors",
                "hover:border-[var(--sf-accent-line)] hover:text-[var(--sf-text)] sf-dim",
                open ? "gap-2.5 px-2" : "justify-center px-0",
              ].join(" ")}
            >
              <Github className="h-[15px] w-[15px] shrink-0" />
              {open && <span className="truncate">Connect GitHub</span>}
            </button>
          </div>
        )}
      </nav>

      <div className="flex flex-col items-center gap-1 border-t sf-line p-2">
        <button
          type="button"
          onClick={onOpenSettings}
          title="Settings"
          aria-label="Settings"
          className="sf-iconbtn h-8 w-8"
        >
          <Settings2 className="h-4 w-4" />
        </button>
        {!hasIndex && (
          <button
            type="button"
            onClick={() => onSelect("repos")}
            title="Add a repository"
            aria-label="Add a repository"
            className="sf-iconbtn h-8 w-8"
          >
            <Plus className="h-4 w-4" />
          </button>
        )}
        {!isTouch && (
          <button
            type="button"
            onClick={() => setPinned((v) => !v)}
            title={pinned ? "Collapse the sidebar" : "Keep the sidebar open"}
            aria-label={pinned ? "Collapse the sidebar" : "Keep the sidebar open"}
            aria-pressed={pinned}
            className="sf-iconbtn h-8 w-8"
          >
            {pinned ? <ChevronsLeft className="h-4 w-4" /> : <ChevronsRight className="h-4 w-4" />}
          </button>
        )}
      </div>
    </div>
  );
}
