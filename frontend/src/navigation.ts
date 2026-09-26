/**
 * navigation.ts — the single source of truth for the app's sections.
 *
 * WHY THIS EXISTS
 * The tab list used to be written out three times: the tab strip in App.tsx,
 * the `g`+letter shortcut map beside it, and a `tabDefs` array inside
 * CommandPalette. Three copies of the same 17 entries, and they had already
 * drifted — CommandPalette drew the Org tab with a `Database` icon while the
 * tab strip used `Building2`, and the two lists were in different orders, so
 * "Go to Org" in the palette did not look like the tab it opened.
 *
 * Every surface that names a section now reads it from here, so a new panel is
 * one entry: the strip, the palette entry, the shortcut, and the reachability
 * test all pick it up together.
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
  FolderTree,
  GitCompare,
  History,
  Layers,
  MessageSquare,
  Network,
  Terminal,
  Wand2,
  Zap,
} from "lucide-react";

export type Tab =
  | "chat"
  | "review"
  | "write"
  | "graph"
  | "health"
  | "org"
  | "agent"
  | "analytics"
  | "prompts"
  | "snippets"
  | "activity"
  | "bulk"
  | "explorer"
  | "diff"
  | "notifications"
  | "slash"
  | "history";

export interface TabDef {
  id: Tab;
  label: string;
  Icon: ElementType;
  /** Tailwind text colour used for the active tab and its underline. */
  color: string;
  /** Second key of the `g <key>` shortcut (e.g. "c" for `g c`). */
  shortcut: string;
}

/**
 * Display order. `agent` sits next to the three "do work" panels (chat, review,
 * write) rather than at the end of the list, where a 17-tab strip puts it off
 * the right edge of a laptop screen.
 */
export const TABS: TabDef[] = [
  { id: "chat", label: "Chat", Icon: MessageSquare, color: "text-blue-400", shortcut: "c" },
  { id: "agent", label: "Agent", Icon: Bot, color: "text-pink-400", shortcut: "a" },
  { id: "review", label: "Code Review", Icon: Zap, color: "text-yellow-400", shortcut: "r" },
  { id: "write", label: "Code Writer", Icon: Wand2, color: "text-green-400", shortcut: "w" },
  { id: "graph", label: "Dep. Graph", Icon: Network, color: "text-purple-400", shortcut: "g" },
  { id: "health", label: "Health", Icon: Activity, color: "text-emerald-400", shortcut: "h" },
  { id: "org", label: "Org", Icon: Building2, color: "text-cyan-400", shortcut: "o" },
  { id: "analytics", label: "Analytics", Icon: BarChart3, color: "text-indigo-400", shortcut: "n" },
  { id: "prompts", label: "Prompts", Icon: Bookmark, color: "text-amber-400", shortcut: "p" },
  { id: "snippets", label: "Snippets", Icon: Code2, color: "text-violet-400", shortcut: "s" },
  { id: "activity", label: "Activity", Icon: Clock, color: "text-teal-400", shortcut: "y" },
  { id: "bulk", label: "Bulk", Icon: Layers, color: "text-blue-400", shortcut: "b" },
  { id: "explorer", label: "Explorer", Icon: FolderTree, color: "text-amber-400", shortcut: "e" },
  { id: "diff", label: "Diff", Icon: GitCompare, color: "text-pink-400", shortcut: "d" },
  { id: "notifications", label: "Inbox", Icon: Bell, color: "text-blue-400", shortcut: "i" },
  { id: "slash", label: "Slash", Icon: Terminal, color: "text-emerald-400", shortcut: "/" },
  { id: "history", label: "History", Icon: History, color: "text-teal-400", shortcut: "t" },
];

export const DEFAULT_TAB: Tab = "chat";

/** `g`+key → tab, derived from TABS so the shortcuts cannot drift from it. */
export const SHORTCUT_BY_KEY: Record<string, Tab> = Object.fromEntries(
  TABS.map((t) => [t.shortcut, t.id]),
) as Record<string, Tab>;
