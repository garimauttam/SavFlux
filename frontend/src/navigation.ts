/**
 * navigation.ts — the single source of truth for the app's sections.
 *
 * WHY THIS FILE WAS REWRITTEN
 * ---------------------------
 * The previous version listed **17** sections as one flat row of tabs. That is
 * the "messed up, non-organized" complaint from users, and it was not a styling
 * problem — it was a structural one:
 *
 *   * 17 tabs do not fit on a laptop, so the strip scrolled and clipped its own
 *     tail behind prev/next arrows. The sections people use least were the ones
 *     hidden, which made the list look arbitrary.
 *   * Several tabs were not sections at all. `Prompts`, `Snippets`, `Activity`,
 *     `History`, `Inbox`, `Slash` and `Bulk` are seven ways to look at small
 *     local collections. Seven tabs for seven small lists is a filing system,
 *     not a product.
 *   * `Chat` and `Agent` split one job in two. "Ask a question" and "go do a
 *     task" are the same conversation with the agent, and the split forced the
 *     user to decide *which product* they were in before they had said anything.
 *
 * So the list is now nine destinations in four groups, and the small
 * collections moved behind a single **Library** destination with sub-tabs. The
 * agent conversation is one surface with a mode switch.
 *
 * Everything else in the app still reads its list from here — the rail, the
 * command palette, the `g`+key shortcuts and the reachability test — so a new
 * destination is one entry, and none of those four can drift apart again.
 */

import type { ElementType } from "react";
import {
  Activity,
  BarChart3,
  Bell,
  Bookmark,
  Bot,
  Building2,
  Clock,
  Code2,
  FileCode,
  FolderTree,
  GitCompare,
  GitPullRequest,
  History,
  Layers,
  Network,
  Search,
  ShieldCheck,
  Terminal,
  Wand2,
  Zap,
  UserRound,
} from "lucide-react";

export type Tab =
  // The agent conversation — ask and do, in one thread.
  | "agent"
  | "review"
  | "write"
  | "explorer"
  | "graph"
  | "health"
  | "repos"
  | "changes"
  | "library"
  | "profile";

/** Sub-destinations inside the Library panel. */
export type LibraryTab =
  | "prompts"
  | "snippets"
  | "activity"
  | "history"
  | "notifications"
  | "slash"
  | "bulk"
  | "analytics";

export interface TabDef {
  id: Tab;
  label: string;
  /** Shown under the icon in the expanded rail, and in the command palette. */
  hint: string;
  Icon: ElementType;
  /** Tailwind text colour for the active destination. */
  color: string;
  /** Second key of the `g`+key shortcut (e.g. "a" for `g a`). */
  shortcut: string;
}

export interface NavGroup {
  id: string;
  label: string;
  items: TabDef[];
}

/**
 * Display order and grouping.
 *
 * The order is the order of a working session: talk to the agent, read what it
 * found, change the code, then look around, then ship. `agent` is first because
 * it is the product, not one feature among seventeen.
 */
export const NAV_GROUPS: NavGroup[] = [
  {
    id: "work",
    label: "Work",
    items: [
      {
        id: "agent",
        label: "Agent",
        hint: "Ask a question or give it a task",
        Icon: Bot,
        color: "text-indigo-400",
        shortcut: "a",
      },
      {
        id: "review",
        label: "Review",
        hint: "Security and quality findings, with line-precise evidence",
        Icon: ShieldCheck,
        color: "text-amber-400",
        shortcut: "r",
      },
      {
        id: "write",
        label: "Write",
        hint: "Generate and apply changes",
        Icon: Wand2,
        color: "text-emerald-400",
        shortcut: "w",
      },
    ],
  },
  {
    id: "understand",
    label: "Understand",
    items: [
      {
        id: "explorer",
        label: "Files",
        hint: "Indexed file tree",
        Icon: FolderTree,
        color: "text-sky-400",
        shortcut: "e",
      },
      {
        id: "graph",
        label: "Graph",
        hint: "Dependency graph and architecture",
        Icon: Network,
        color: "text-violet-400",
        shortcut: "g",
      },
      {
        id: "health",
        label: "Health",
        hint: "Dependency, CVE and secret posture",
        Icon: Activity,
        color: "text-rose-400",
        shortcut: "h",
      },
    ],
  },
  {
    id: "ship",
    label: "Ship",
    items: [
      {
        id: "repos",
        label: "Repositories",
        hint: "Connect GitHub, browse and index repositories",
        Icon: GitPullRequest,
        color: "text-purple-400",
        shortcut: "o",
      },
      {
        id: "changes",
        label: "Changes",
        hint: "Compare remote branches, Agent workspaces or indexed snapshots",
        Icon: GitCompare,
        color: "text-teal-400",
        shortcut: "d",
      },
    ],
  },
  {
    id: "keep",
    label: "Keep",
    items: [
      {
        id: "library",
        label: "Library",
        hint: "Prompts, snippets, history and activity",
        Icon: Layers,
        color: "text-orange-400",
        shortcut: "l",
      },
    ],
  },
  {
    id: "account", label: "Account", items: [{
      id: "profile", label: "Profile", hint: "Account, connections, models and privacy",
      Icon: UserRound, color: "text-indigo-400", shortcut: "p",
    }],
  },
];



/** Flat view of every destination, in rail order. */
export const TABS: TabDef[] = NAV_GROUPS.flatMap((g) => g.items);

export const DEFAULT_TAB: Tab = "agent";

/** `g`+key → destination, derived from TABS so the shortcuts cannot drift. */
export const SHORTCUT_BY_KEY: Record<string, Tab> = Object.fromEntries(
  TABS.map((t) => [t.shortcut, t.id]),
) as Record<string, Tab>;

/* ── Library sub-tabs ──────────────────────────────────────────────────────── */

export interface LibraryTabDef {
  id: LibraryTab;
  label: string;
  Icon: ElementType;
}

export const LIBRARY_TABS: LibraryTabDef[] = [
  { id: "prompts", label: "Prompts", Icon: Bookmark },
  { id: "snippets", label: "Snippets", Icon: Code2 },
  { id: "history", label: "History", Icon: History },
  { id: "activity", label: "Activity", Icon: Clock },
  { id: "notifications", label: "Inbox", Icon: Bell },
  { id: "slash", label: "Commands", Icon: Terminal },
  { id: "bulk", label: "Bulk", Icon: Layers },
  { id: "analytics", label: "Metrics", Icon: BarChart3 },
];

/* ── Icons re-exported for surfaces that need one without a TabDef ─────────── */

export { FileCode, Search, Building2, Zap };
