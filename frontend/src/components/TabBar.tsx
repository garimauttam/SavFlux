/**
 * TabBar.tsx — the section strip across the top of the app.
 *
 * THE BUG THIS REPLACES
 * SavFlux has 17 sections. The old strip was a plain `flex` row inside a header
 * with no overflow handling, and the sidebar takes 288px (`w-72`). Each tab is
 * an icon + label + 32px of padding, so the strip needs well over 1700px. On a
 * 1440px laptop the tabs past "Prompts" were clipped with no scrollbar, no
 * arrows and no way to reach them by mouse — the only route left was a keyboard
 * shortcut nobody knew existed (the shortcuts were not documented on screen
 * either, only behind `?`).
 *
 * So the strip is now a real scrollable tablist:
 *   - `overflow-x-auto` with the scrollbar hidden, plus edge fades and prev/next
 *     buttons that appear only when there is something to scroll to;
 *   - the active tab is scrolled into view when it changes from anywhere else
 *     (⌘K palette, `g` shortcuts), so selection never lands off-screen;
 *   - `role="tablist"` / `role="tab"` with `aria-selected`, a roving tabindex
 *     and Arrow/Home/End navigation, so the strip is usable without a mouse.
 *
 * The strip owns presentation and keyboard handling only; which panel is shown
 * is still App's state.
 */

import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type KeyboardEvent,
} from "react";
import { ChevronLeft, ChevronRight } from "lucide-react";
import { TABS, type Tab, type TabDef } from "../navigation";

/** One press of an arrow moves about two tabs' worth of strip. */
const SCROLL_STEP_PX = 240;

interface TabBarProps {
  /** Defaults to the app's real section list; tests pass a short one. */
  tabs?: TabDef[];
  activeTab: Tab;
  onSelect: (tab: Tab) => void;
}

export function TabBar({ tabs = TABS, activeTab, onSelect }: TabBarProps) {
  const scrollerRef = useRef<HTMLDivElement>(null);
  // Which direction the strip can still scroll, measured from the DOM rather
  // than guessed from the tab count: label widths depend on the font loaded.
  const [edges, setEdges] = useState({ canLeft: false, canRight: false });

  const measure = useCallback(() => {
    const el = scrollerRef.current;
    if (!el) return;
    const scrollable = el.scrollWidth - el.clientWidth > 1;
    setEdges({
      canLeft: scrollable && el.scrollLeft > 1,
      canRight: scrollable && el.scrollLeft < el.scrollWidth - el.clientWidth - 1,
    });
  }, []);

  // Measure on mount, and again whenever the strip is resized (window resize,
  // sidebar collapse). Without ResizeObserver the arrows stay hidden after the
  // first layout even though the strip has become scrollable.
  useLayoutEffect(measure, [measure]);
  useEffect(() => {
    const el = scrollerRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [measure]);

  // Keep the selected tab visible. Selection can change from the palette or a
  // keyboard shortcut, in which case the tab may be scrolled out of view.
  useEffect(() => {
    const el = scrollerRef.current?.querySelector<HTMLElement>(`[data-tab-id="${activeTab}"]`);
    el?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }, [activeTab]);

  const scrollBy = (direction: 1 | -1) => {
    scrollerRef.current?.scrollBy({ left: direction * SCROLL_STEP_PX, behavior: "smooth" });
  };

  const selectAt = useCallback(
    (index: number) => {
      const count = tabs.length;
      if (count === 0) return;
      const wrapped = ((index % count) + count) % count;
      const tab = tabs[wrapped];
      onSelect(tab.id);
      // Roving tabindex: focus follows selection, so the arrow keys keep working
      // without the focus ring being stranded on the previous tab.
      scrollerRef.current?.querySelector<HTMLElement>(`[data-tab-id="${tab.id}"]`)?.focus();
    },
    [tabs, onSelect],
  );

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    const index = tabs.findIndex((t) => t.id === activeTab);
    const moves: Record<string, number> = {
      ArrowRight: index + 1,
      ArrowLeft: index - 1,
      Home: 0,
      End: tabs.length - 1,
    };
    if (!(e.key in moves)) return;
    e.preventDefault();
    selectAt(moves[e.key]);
  };

  // Solid, not a gradient: the light-theme layer in index.css remaps
  // `bg-gray-900` but cannot reach gradient stop utilities, so a gradient would
  // stay dark when the rest of the header turns white.
  const arrowButton =
    "absolute top-0 bottom-0 z-10 flex w-9 items-center justify-center bg-gray-900 text-gray-400 hover:text-gray-200";

  return (
    <div className="relative flex min-w-0 flex-1 items-stretch" data-testid="tabbar">
      {edges.canLeft && (
        <button
          type="button"
          aria-label="Scroll tabs left"
          data-testid="tabbar-scroll-left"
          onClick={() => scrollBy(-1)}
          className={`${arrowButton} left-0 border-r border-gray-700`}
        >
          <ChevronLeft className="h-4 w-4" />
        </button>
      )}

      <div
        ref={scrollerRef}
        role="tablist"
        aria-label="SavFlux sections"
        aria-orientation="horizontal"
        data-testid="tabbar-scroller"
        onScroll={measure}
        onKeyDown={onKeyDown}
        className="flex min-w-0 flex-1 items-stretch overflow-x-auto scrollbar-none"
      >
        {tabs.map((tab) => {
          const selected = tab.id === activeTab;
          return (
            <button
              key={tab.id}
              type="button"
              role="tab"
              id={`savflux-tab-${tab.id}`}
              data-tab-id={tab.id}
              aria-selected={selected}
              aria-controls={`savflux-panel-${tab.id}`}
              tabIndex={selected ? 0 : -1}
              title={tab.shortcut ? `${tab.label} — g ${tab.shortcut}` : tab.label}
              onClick={() => onSelect(tab.id)}
              className={`flex shrink-0 items-center gap-1.5 whitespace-nowrap border-b-2 px-4 py-3 text-sm font-medium transition-colors ${
                selected
                  ? `${tab.color} border-current`
                  : "border-transparent text-gray-500 hover:text-gray-300"
              }`}
            >
              <tab.Icon className="h-4 w-4 shrink-0" />
              {tab.label}
            </button>
          );
        })}
      </div>

      {edges.canRight && (
        <button
          type="button"
          aria-label="Scroll tabs right"
          data-testid="tabbar-scroll-right"
          onClick={() => scrollBy(1)}
          className={`${arrowButton} right-0 border-l border-gray-700`}
        >
          <ChevronRight className="h-4 w-4" />
        </button>
      )}
    </div>
  );
}

export default TabBar;
