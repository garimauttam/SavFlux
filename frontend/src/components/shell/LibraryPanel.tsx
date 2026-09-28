/**
 * LibraryPanel.tsx — eight small collections, one destination.
 *
 * WHY THEY WERE MERGED
 * --------------------
 * Prompts, Snippets, History, Activity, Inbox, Commands, Bulk and Metrics each
 * had their own tab: eight entries in the navigation bar, eight switches in
 * App, eight ways to be "in a panel" that happened to contain a list. Eight
 * tabs for eight lists is a filing system. It is also the reason the tab strip
 * could not fit on a screen, which is what made the whole app look assembled
 * rather than designed.
 *
 * They are still eight lists, all of them still fully functional. What changed
 * is that finding one is a click inside a panel whose contents you can see,
 * rather than hunting a strip for a label you had to already know.
 */

import { useState } from "react";
import { LIBRARY_TABS, type LibraryTab } from "../../navigation";
import AnalyticsPanel from "../AnalyticsPanel";
import PromptLibrary from "../PromptLibrary";
import SnippetVault from "../SnippetVault";
import ActivityFeed from "../ActivityFeed";
import NotificationsPanel from "../NotificationsPanel";
import SlashCommandsPanel from "../SlashCommandsPanel";
import { TimeMachinePanel } from "../TimeMachinePanel";
import BulkOpsPanel from "../BulkOpsPanel";

interface LibraryPanelProps {
  onUsePrompt: (text: string) => void;
  /** Re-read the index — the History tab can index from its own empty state. */
  onIndexed: () => void;
}

export function LibraryPanel({ onUsePrompt, onIndexed }: LibraryPanelProps) {
  const [tab, setTab] = useState<LibraryTab>("prompts");

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div
        role="tablist"
        aria-label="Library"
        className="sf-surface scrollbar-none flex shrink-0 items-center gap-1 overflow-x-auto border-b sf-line px-3 py-2"
      >
        {LIBRARY_TABS.map((t) => {
          const Icon = t.Icon;
          const selected = t.id === tab;
          return (
            <button
              key={t.id}
              role="tab"
              aria-selected={selected}
              aria-label={t.label}
              onClick={() => setTab(t.id)}
              className={[
                "flex shrink-0 items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-[12.5px] font-medium transition-colors",
                selected
                  ? "sf-accent-soft sf-accent"
                  : "sf-dim hover:bg-[var(--sf-raised)] hover:text-[var(--sf-text)]",
              ].join(" ")}
            >
              <Icon className="h-3.5 w-3.5" />
              {t.label}
            </button>
          );
        })}
      </div>

      <div
        role="tabpanel"
        id={`savflux-library-${tab}`}
        aria-labelledby={`savflux-library-tab-${tab}`}
        className="min-h-0 flex-1 overflow-hidden"
      >
        {tab === "prompts" && <PromptLibrary onUsePrompt={onUsePrompt} />}
        {tab === "snippets" && <SnippetVault />}
        {tab === "history" && <TimeMachinePanel onIndexed={onIndexed} />}
        {tab === "activity" && <ActivityFeed />}
        {tab === "notifications" && <NotificationsPanel />}
        {tab === "slash" && <SlashCommandsPanel onUse={onUsePrompt} />}
        {tab === "bulk" && <BulkOpsPanel onFilesUpdated={() => window.location.reload()} />}
        {tab === "analytics" && <AnalyticsPanel />}
      </div>
    </div>
  );
}
