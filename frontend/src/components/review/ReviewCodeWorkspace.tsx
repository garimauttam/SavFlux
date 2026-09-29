import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ChevronLeft,
  ChevronRight,
  FileCode,
  ListFilter,
  Loader2,
  MessageSquare,
  PanelLeft,
  PanelRight,
  Search,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { ReviewSection } from "../../hooks/useMultiReview";
import { reviewPath } from "../../lib/reviewFindings";
import { ReviewFileTree } from "./ReviewFileTree";
import { ReviewFileCard, type ReviewCardHandle } from "./ReviewFileCard";
import { reviewOutcome } from "./reviewOutcome";
export { reviewOutcome } from "./reviewOutcome";

export function ReviewCodeWorkspace({
  sections,
  active,
  currentStep,
  error,
  stopped,
}: {
  sections: ReviewSection[];
  active: boolean;
  currentStep: string | null;
  error: string | null;
  stopped: boolean;
}) {
  const files = useMemo(
    () => sections.filter((s) => s.id !== "__repo_summary__"),
    [sections],
  );
  const summary = sections.find((s) => s.id === "__repo_summary__");
  const [selected, setSelected] = useState("");
  const [target, setTarget] = useState("");
  const [query, setQuery] = useState("");
  const [onlyFindings, setOnlyFindings] = useState(false);
  const [filesOpen, setFilesOpen] = useState(() => window.innerWidth >= 900);
  const [notesOpen, setNotesOpen] = useState(false);
  const [showSummary, setShowSummary] = useState(false);
  const [resolved, setResolved] = useState<Set<string>>(new Set());
  const [canJump, setCanJump] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);
  const cards = useRef<Map<string, ReviewCardHandle>>(new Map());
  const nodes = useRef<Map<string, HTMLDivElement>>(new Map());
  const visibleFiles = files.filter(
    (f) =>
      (!onlyFindings || (f.findings?.length ?? 0) > 0) &&
      `${f.fileName} ${f.id}`.toLowerCase().includes(query.toLowerCase()),
  );
  const file = visibleFiles.find((f) => f.id === selected) ?? visibleFiles[0];
  const fileId = file?.id;
  const pendingNavigation = useRef("");
  const onReady = useCallback(
    (source: string, loaded: boolean) => {
      setCanJump(cards.current.get(fileId ?? "")?.canJump() ?? false);
      if (loaded && pendingNavigation.current === source) {
        pendingNavigation.current = "";
        requestAnimationFrame(() =>
          nodes.current
            .get(source)
            ?.scrollIntoView({ block: "start", behavior: "auto" }),
        );
      }
    },
    [fileId],
  );
  useEffect(
    () => setCanJump(cards.current.get(fileId ?? "")?.canJump() ?? false),
    [fileId],
  );
  const total = files.reduce((n, f) => n + (f.findings?.length ?? 0), 0);
  const completed = files.filter((f) =>
    ["complete", "error", "skipped"].includes(f.status),
  ).length;
  const navigate = (id: string) => {
    pendingNavigation.current = id;
    setSelected(id);
    setTarget(id);
    setShowSummary(false);
    cards.current.get(id)?.reveal();
    if (window.innerWidth < 800) setFilesOpen(false);
    nodes.current.get(id)?.scrollIntoView({ block: "start", behavior: "auto" });
  };
  // Scrolling chooses the file occupying the reading line, not the last file loaded.
  // Throttle with rAF: hundreds of review files must not cause a React update per pixel.
  useEffect(() => {
    const root = scrollRef.current;
    if (!root) return;
    let frame = 0;
    const handle = () => {
      if (frame) return;
      frame = requestAnimationFrame(() => {
        frame = 0;
        const readingLine = root.getBoundingClientRect().top + 70;
        const next = visibleFiles.find((f) => {
          const box = nodes.current.get(f.id)?.getBoundingClientRect();
          return box && box.bottom > readingLine;
        });
        if (next) setSelected(next.id);
      });
    };
    root.addEventListener("scroll", handle, { passive: true });
    return () => {
      root.removeEventListener("scroll", handle);
      cancelAnimationFrame(frame);
    };
  }, [visibleFiles.map((f) => f.id).join("\n")]);
  const onResolve = (key: string) =>
    setResolved((old) => {
      const next = new Set(old);
      next.has(key) ? next.delete(key) : next.add(key);
      return next;
    });
  return (
    <section
      aria-label="Code review workspace"
      className="sf-review-workspace flex h-full min-h-0 min-w-0 flex-1 flex-col sf-surface"
    >
      <div className="flex shrink-0 flex-wrap items-center gap-2 border-b sf-line px-3 py-2 text-xs">
        {active ? (
          <Loader2 size={14} className="animate-spin sf-accent" />
        ) : (
          <MessageSquare size={14} className="sf-accent" />
        )}
        <span className="sf-text font-medium">
          {stopped ? "Stopped" : active ? "Reviewing" : "Review results"}
        </span>
        <span className="sf-mute">
          {completed}/{files.length} files · {total} comment
          {total === 1 ? "" : "s"}
        </span>
        <span
          title={currentStep ?? ""}
          className="sf-mute hidden min-w-0 flex-1 truncate lg:block"
        >
          {active
            ? currentStep
            : "Scroll through files · suggestions never change your source"}
        </span>
        <button
          className="sf-btn sf-btn-ghost ml-auto text-xs"
          onClick={() => {
            setShowSummary(!showSummary);
            setNotesOpen(true);
          }}
        >
          {showSummary ? "File notes" : "Run summary"}
        </button>
      </div>
      {error && (
        <div role="alert" className="border-b sf-line p-3 text-xs text-red-400">
          {error}
        </div>
      )}
      <div className="relative flex min-h-0 flex-1">
        <aside
          aria-label="Reviewed files"
          aria-hidden={!filesOpen}
          style={{
            width: filesOpen ? 256 : 0,
            opacity: filesOpen ? 1 : 0,
            visibility: filesOpen ? "visible" : "hidden",
          }}
          className="sf-review-files sf-collapse sf-surface flex shrink-0 flex-col border-r sf-line"
        >
          <div
            className="space-y-2 border-b sf-line p-3"
            style={{ width: 256 }}
          >
            <div className="sf-dim flex items-center gap-2 text-xs font-medium">
              <FileCode size={14} /> Files in this review
              <button
                className="sf-iconbtn ml-auto h-6 w-6"
                aria-label="Close file list"
                onClick={() => setFilesOpen(false)}
              >
                <PanelLeft size={13} />
              </button>
            </div>
            <div className="relative">
              <Search size={13} className="sf-mute absolute left-2 top-2.5" />
              <input
                aria-label="Find reviewed file"
                className="sf-input"
                style={{ paddingLeft: 28 }}
                placeholder="Find a file…"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
              />
            </div>
            <button
              aria-pressed={onlyFindings}
              className={`sf-btn w-full text-xs ${onlyFindings ? "sf-accent-soft sf-accent" : "sf-btn-ghost"}`}
              onClick={() => setOnlyFindings(!onlyFindings)}
            >
              <ListFilter size={13} />
              Only files with comments
            </button>
          </div>
          <div
            data-file-tree-scroll
            className="min-h-0 flex-1 overflow-auto py-2"
            style={{ width: 256 }}
          >
            <ReviewFileTree
              files={visibleFiles}
              selected={fileId ?? ""}
              onSelect={navigate}
              outcome={reviewOutcome}
            />
            {!visibleFiles.length && (
              <p className="sf-mute p-3 text-xs">
                No matching files{active ? " yet" : ""}.
              </p>
            )}
          </div>
        </aside>
        <div className="flex min-w-0 flex-1 flex-col">
          <div className="sf-raised flex min-h-11 shrink-0 items-center gap-2 border-b sf-line p-2">
            <button
              className="sf-iconbtn h-7 w-7 shrink-0"
              aria-expanded={filesOpen}
              aria-label={
                filesOpen ? "Hide reviewed files" : "Show reviewed files"
              }
              onClick={() => setFilesOpen(!filesOpen)}
            >
              <PanelLeft size={15} />
            </button>
            <span
              className="sf-text min-w-0 flex-1 truncate font-mono text-xs"
              title={fileId}
            >
              {fileId ? reviewPath(fileId) : "Files changed / reviewed"}
            </span>
            <button
              className="sf-iconbtn h-7 w-7"
              aria-label="Previous comment"
              disabled={!canJump}
              onClick={() => cards.current.get(fileId ?? "")?.jump(-1)}
            >
              <ChevronLeft size={14} />
            </button>
            <button
              className="sf-iconbtn h-7 w-7"
              aria-label="Next comment"
              disabled={!canJump}
              onClick={() => cards.current.get(fileId ?? "")?.jump(1)}
            >
              <ChevronRight size={14} />
            </button>
            <button
              className="sf-iconbtn h-7 w-7"
              aria-expanded={notesOpen}
              aria-label={notesOpen ? "Hide review notes" : "Show review notes"}
              onClick={() => setNotesOpen(!notesOpen)}
            >
              <PanelRight size={15} />
            </button>
          </div>
          <div
            ref={scrollRef}
            aria-label="Continuous file review"
            className="sf-review-scroll min-h-0 flex-1 overflow-y-auto p-3"
          >
            {visibleFiles.map((f, i) => (
              <div
                key={f.id}
                ref={(el) => {
                  if (el) nodes.current.set(f.id, el);
                  else nodes.current.delete(f.id);
                }}
                className="sf-file-anchor"
              >
                <ReviewFileCard
                  ref={(el) => {
                    if (el) cards.current.set(f.id, el);
                    else cards.current.delete(f.id);
                  }}
                  file={f}
                  eager={i === 0 || target === f.id}
                  active={fileId === f.id}
                  reviewActive={active}
                  root={scrollRef}
                  onReady={onReady}
                  onNotes={() => {
                    setSelected(f.id);
                    setShowSummary(false);
                    setNotesOpen(true);
                  }}
                  isResolved={(key) => resolved.has(key)}
                  onResolve={onResolve}
                />
              </div>
            ))}
            {!visibleFiles.length && (
              <p className="sf-mute p-6 text-sm">
                No matching files. Clear the search or comment filter to see the
                rest of the review.
              </p>
            )}
          </div>
        </div>
        <aside
          aria-label="Review notes"
          aria-hidden={!notesOpen}
          className="sf-review-notes sf-collapse sf-surface flex shrink-0 flex-col border-l sf-line"
          style={{
            width: notesOpen ? "min(360px,38vw)" : 0,
            opacity: notesOpen ? 1 : 0,
            visibility: notesOpen ? "visible" : "hidden",
          }}
        >
          <div className="sf-raised flex h-11 shrink-0 items-center justify-between border-b sf-line px-3">
            <h3 className="sf-text text-xs font-medium">
              {showSummary ? "Run summary" : "File discussion"}
            </h3>
            <button
              className="sf-iconbtn h-7 w-7"
              aria-label="Close review notes"
              onClick={() => setNotesOpen(false)}
            >
              <PanelRight size={15} />
            </button>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto p-4">
            <p className="sf-mute mb-4 text-xs">
              {showSummary
                ? "Cross-file conclusions from this run."
                : "Full report. Unverified locations remain file-level notes."}
            </p>
            <div className="prose prose-invert prose-sm max-w-none break-words text-xs">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>
                {(showSummary ? summary?.content : file?.content) ||
                  (active
                    ? "The review is still running. Model notes will arrive here."
                    : "No report was produced for this view.")}
              </ReactMarkdown>
            </div>
          </div>
        </aside>
      </div>
    </section>
  );
}
