/** Navigation stays at the width explicitly chosen by the user, including on touch. */

import { useEffect, useId, useState } from "react";
import {
  Github,
  PanelLeftClose,
  PanelLeftOpen,
  Plus,
  UserRound,
} from "lucide-react";
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
  const navigationId = useId();
  // Layout preference only; no review or source data is persisted.
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
  // Explicit controls work on mouse, keyboard and touch; hover never reopens it.
  const open = pinned;

  useEffect(() => {
    try {
      localStorage.setItem("savflux:railPinned", pinned ? "1" : "0");
    } catch {
      /* private mode — the preference just does not persist */
    }
  }, [pinned]);

  return (
    <div
      data-testid="activity-rail"
      data-expanded={open ? "true" : "false"}
      className="sf-surface flex shrink-0 flex-col border-r sf-line transition-[width] duration-200 motion-reduce:transition-none ease-out"
      style={{ width: open ? "var(--sf-rail-open)" : "var(--sf-rail)" }}
    >
      {/* Wordmark — the product name, not a tab. */}
      <div className="flex h-12 items-center gap-2 border-b sf-line px-3">
        <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-indigo-500/15 text-[13px] font-bold text-indigo-300">
          S
        </div>
        {open && (
          <span className="sf-text truncate text-[13px] font-semibold tracking-tight">
            SavFlux
          </span>
        )}
      </div>

      {/* Always visible, even when the navigation scrolls on a short screen. */}
      <div
        className={`flex h-11 shrink-0 items-center ${open ? "justify-between pl-4 pr-2" : "justify-center"}`}
      >
        {open && (
          <span className="sf-mute text-[10px] font-semibold uppercase tracking-[0.08em]">
            Work
          </span>
        )}
        <button
          type="button"
          onClick={() => setPinned((v) => !v)}
          title={open ? "Collapse sidebar" : "Expand sidebar"}
          aria-label={open ? "Collapse sidebar" : "Expand sidebar"}
          aria-expanded={open}
          aria-controls={navigationId}
          className="sf-iconbtn h-9 w-9 shrink-0"
        >
          {open ? (
            <PanelLeftClose aria-hidden className="h-4 w-4" />
          ) : (
            <PanelLeftOpen aria-hidden className="h-4 w-4" />
          )}
        </button>
      </div>

      <nav
        id={navigationId}
        aria-label="Sections"
        className="scrollbar-none min-h-0 flex-1 overflow-y-auto overflow-x-hidden pb-2"
      >
        {/* Profile has a single, persistent home in the footer below. */}
        {NAV_GROUPS.filter((group) => group.id !== "account").map((group) => (
          <div key={group.id} className="mb-1.5 px-2">
            {open && group.id !== "work" && (
              <div className="sf-mute px-2 pb-1 pt-2 text-[10px] font-semibold uppercase tracking-[0.08em]">
                {group.label}
              </div>
            )}
            {open && group.id !== "work" && (
              <div className="mx-2 mb-1 h-px sf-line" />
            )}
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

      <div className="flex shrink-0 flex-col gap-1 border-t sf-line p-2">
        {!hasIndex && (
          <button
            type="button"
            onClick={() => onSelect("repos")}
            title="Add a repository"
            aria-label="Add a repository"
            className={`sf-iconbtn h-9 w-full text-xs ${open ? "justify-start gap-2.5 px-2" : "justify-center"}`}
          >
            <Plus aria-hidden className="h-4 w-4 shrink-0" />
            {open && <span>Add repository</span>}
          </button>
        )}
        <button
          type="button"
          role="tab"
          aria-selected={active === "profile"}
          onClick={onOpenSettings}
          title="Profile — account & settings"
          aria-label="Profile"
          className={[
            "flex min-h-12 w-full items-center rounded-lg text-left transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--sf-accent)]",
            open ? "gap-2.5 px-2" : "justify-center",
            active === "profile"
              ? "sf-accent-soft text-[var(--sf-accent)]"
              : "sf-dim hover:bg-[var(--sf-raised)] hover:text-[var(--sf-text)]",
          ].join(" ")}
        >
          <span className="sf-accent-soft sf-accent flex h-8 w-8 shrink-0 items-center justify-center rounded-full border border-[var(--sf-accent-line)]">
            <UserRound aria-hidden className="h-4 w-4" />
          </span>
          {open && (
            <span className="min-w-0">
              <span className="block text-[13px] font-medium">Profile</span>
              <span className="sf-mute block truncate text-[11px]">
                Account & settings
              </span>
            </span>
          )}
        </button>
      </div>
    </div>
  );
}
