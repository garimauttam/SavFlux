import {
  Fragment,
  forwardRef,
  useEffect,
  useImperativeHandle,
  useMemo,
  useRef,
  useState,
  type RefObject,
} from "react";
import {
  AlertTriangle,
  ChevronDown,
  ChevronRight,
  FileCode,
  Loader2,
  MessageSquare,
} from "lucide-react";
import { apiFetch } from "../../api";
import { ReviewSyntax } from "../../lib/highlight";
import {
  codeRows,
  findingKey,
  reviewPath,
  type ReviewFinding,
} from "../../lib/reviewFindings";
import type { ReviewSection } from "../../hooks/useMultiReview";
import { ReviewComment } from "./ReviewComment";
import { reviewOutcome } from "./reviewOutcome";

export interface ReviewCardHandle {
  jump: (delta: number) => void;
  canJump: () => boolean;
  reveal: () => void;
}
export const ReviewFileCard = forwardRef<
  ReviewCardHandle,
  {
    file: ReviewSection;
    eager: boolean;
    active: boolean;
    reviewActive?: boolean;
    root: RefObject<HTMLDivElement>;
    onNotes: () => void;
    onReady: (source: string, loaded: boolean) => void;
    isResolved: (key: string) => boolean;
    onResolve: (key: string) => void;
  }
>(function ReviewFileCard(
  {
    file,
    eager,
    active,
    reviewActive = false,
    root,
    onNotes,
    onReady,
    isResolved,
    onResolve,
  },
  ref,
) {
  const host = useRef<HTMLElement>(null);
  const [visible, setVisible] = useState(
    eager || typeof IntersectionObserver === "undefined",
  );
  const [folded, setFolded] = useState(false);
  const [loaded, setLoaded] = useState<{
    content: string;
    hash: string;
  } | null>(null);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  const [all, setAll] = useState(false);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [focus, setFocus] = useState("");
  const anchors = useRef<Map<string, HTMLDivElement>>(new Map());
  const commentIndex = useRef(-1);
  const source = file.id;
  useEffect(() => {
    if (eager) {
      setVisible(true);
      setFolded(false);
    }
  }, [eager]);
  useEffect(() => {
    if (visible || !host.current || typeof IntersectionObserver === "undefined")
      return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) {
          setVisible(true);
          observer.disconnect();
        }
      },
      { root: root.current, rootMargin: "400px 0px" },
    );
    observer.observe(host.current);
    return () => observer.disconnect();
  }, [visible, root]);
  useEffect(() => {
    if (!visible) return;
    const controller = new AbortController();
    setLoaded(null);
    setError("");
    const timer = window.setTimeout(() => {
      setError("Loading source timed out. Retry when the index is available.");
      controller.abort();
    }, 20000);
    void (async () => {
      try {
        const res = await apiFetch(
          `/api/v1/write/file-content?source=${encodeURIComponent(source)}`,
          { signal: controller.signal },
        );
        if (!res.ok)
          throw new Error(
            res.status === 404
              ? "This file is no longer indexed. Re-index and run a new review."
              : `Could not load code (HTTP ${res.status}).`,
          );
        const data = await res.json();
        if (typeof data.content !== "string")
          throw new Error("The server did not return source code.");
        const digest = globalThis.crypto?.subtle
          ? await crypto.subtle.digest(
              "SHA-256",
              new TextEncoder().encode(data.content),
            )
          : null;
        const hash = digest
          ? Array.from(new Uint8Array(digest), (b) =>
              b.toString(16).padStart(2, "0"),
            ).join("")
          : "";
        if (!controller.signal.aborted)
          setLoaded({ content: data.content, hash });
      } catch (e) {
        if (!controller.signal.aborted)
          setError(e instanceof Error ? e.message : "Could not load code.");
      } finally {
        window.clearTimeout(timer);
      }
    })();
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [source, visible, retry]);
  const lines = useMemo(() => loaded?.content.split("\n") ?? [], [loaded]);
  const verified = Boolean(
    file.analysis?.contentHash && file.analysis.contentHash === loaded?.hash,
  );
  const findings = file.findings ?? [];
  const anchored = verified
    ? findings.filter(
        (f) => f.line <= lines.length && (f.endLine ?? f.line) <= lines.length,
      )
    : [];
  const unanchored = findings.filter((f) => !anchored.includes(f));
  const rows = codeRows(lines.length, anchored, expanded, all);
  const language = (
    reviewPath(source).split(".").pop() || "text"
  ).toLowerCase();
  const range = anchored.find((f) => findingKey(f) === focus);
  useImperativeHandle(ref, () => ({
    canJump: () => anchored.length > 0,
    reveal: () => {
      setVisible(true);
      setFolded(false);
    },
    jump: (delta) => {
      if (!anchored.length) return;
      setFolded(false);
      commentIndex.current =
        (commentIndex.current + delta + anchored.length) % anchored.length;
      const key = findingKey(anchored[commentIndex.current]);
      setFocus(key);
      requestAnimationFrame(() =>
        anchors.current.get(key)?.scrollIntoView({
          block: "center",
          behavior: window.matchMedia?.("(prefers-reduced-motion: reduce)")
            .matches
            ? "auto"
            : "smooth",
        }),
      );
    },
  }));
  useEffect(() => {
    onReady(source, Boolean(loaded));
  }, [source, loaded, file.analysis, file.findings, onReady]);
  const renderComment = (f: ReviewFinding, located: boolean) => {
    const key = `${source}:${file.analysis?.contentHash}:${findingKey(f)}`;
    return (
      <ReviewComment
        finding={f}
        source={source}
        content={loaded?.content ?? ""}
        hash={loaded?.hash ?? ""}
        language={language}
        verified={located}
        reviewActive={reviewActive}
        resolved={isResolved(key)}
        onResolve={() => onResolve(key)}
        onFocus={() => setFocus(findingKey(f))}
      />
    );
  };
  return (
    <section
      ref={host}
      data-review-source={source}
      aria-label={`Review of ${reviewPath(source)}`}
      className={`sf-file-review ${active ? "is-current" : ""}`}
    >
      <header className="sf-file-header">
        <button
          className="sf-iconbtn h-7 w-7"
          aria-label={`${folded ? "Expand" : "Collapse"} ${reviewPath(source)}`}
          aria-expanded={!folded}
          onClick={() => setFolded(!folded)}
        >
          {folded ? <ChevronRight size={14} /> : <ChevronDown size={14} />}
        </button>
        <FileCode size={14} className="sf-mute shrink-0" />
        <h3
          className="sf-text min-w-0 flex-1 truncate font-mono text-xs"
          title={source}
        >
          {reviewPath(source)}
        </h3>
        <span className="sf-mute text-[10px]">{reviewOutcome(file)}</span>
        <button
          title="Open file report"
          aria-label={`Review notes for ${reviewPath(source)}`}
          onClick={onNotes}
          className="sf-btn sf-btn-ghost text-xs"
        >
          <MessageSquare size={13} />
          {findings.length}
        </button>
      </header>
      {!folded && (
        <>
          <div className="sf-file-meta">
            <span>Indexed snapshot · lines refer to this snapshot</span>
            <button onClick={() => setAll(!all)}>
              {all ? "Collapse context" : "Show full file"}
            </button>
          </div>
          {!visible && (
            <div className="sf-mute sf-file-placeholder">
              Scroll here to load source, or select this file in the tree.
            </div>
          )}
          {visible && !loaded && !error && (
            <div className="sf-mute sf-file-placeholder">
              <Loader2 size={14} className="animate-spin" /> Loading indexed
              source…
            </div>
          )}
          {error && (
            <div role="alert" className="p-4 text-xs text-amber-500">
              {error}
              <button
                className="sf-btn sf-btn-secondary mt-2"
                onClick={() => setRetry((v) => v + 1)}
              >
                Retry loading code
              </button>
            </div>
          )}
          {file.fallbackReason && (
            <p role="status" className="sf-review-warning">
              <AlertTriangle size={14} /> Model unavailable:{" "}
              {file.fallbackReason}. Static findings only.
            </p>
          )}
          {file.analysis?.parseError && (
            <p className="sf-review-warning">
              Partial analysis: {file.analysis.parseError}. A reliable health
              score cannot be assigned.
            </p>
          )}
          {loaded && file.analysis && !verified && (
            <p role="alert" className="sf-review-warning">
              The source snapshot could not be matched to this review. Comments
              are kept separate from code; run a fresh review before generating
              a fix.
            </p>
          )}
          {loaded && !file.analysis && (
            <p className="sf-mute p-3 text-xs">
              Waiting for line findings. Reports without source anchors stay in
              review notes.
            </p>
          )}
          {loaded && (
            <div className="sf-file-code">
              <ReviewSyntax
                code={loaded.content}
                language={language}
                renderLines={(tokens) =>
                  rows.map((row) =>
                    row.kind === "gap" ? (
                      <button
                        key={`gap-${row.start}`}
                        className="sf-context-gap"
                        onClick={() =>
                          setExpanded(
                            (old) =>
                              new Set([...old, `${row.start}:${row.end}`]),
                          )
                        }
                      >
                        ↕ Show {row.end - row.start + 1} hidden lines (
                        {row.start}–{row.end})
                      </button>
                    ) : (
                      <Fragment key={row.line}>
                        <div
                          data-line={row.line}
                          className={`sf-code-line ${anchored.some((f) => row.line >= f.line && row.line <= (f.endLine ?? f.line)) ? "is-issue" : ""} ${range && row.line >= range.line && row.line <= (range.endLine ?? range.line) ? "is-focused" : ""}`}
                        >
                          <span className="sf-line-number">{row.line}</span>
                          <code>
                            {tokens[row.line - 1] ?? lines[row.line - 1] ?? " "}
                          </code>
                        </div>
                        {anchored
                          .filter((f) => (f.endLine ?? f.line) === row.line)
                          .map((f) => (
                            <div
                              key={findingKey(f)}
                              ref={(el) => {
                                if (el) anchors.current.set(findingKey(f), el);
                                else anchors.current.delete(findingKey(f));
                              }}
                            >
                              {renderComment(f, true)}
                            </div>
                          ))}
                      </Fragment>
                    ),
                  )
                }
              />
            </div>
          )}
          {loaded && !findings.length && file.analysis && (
            <p className="sf-mute border-t sf-line p-3 text-xs">
              No inline findings reported. This is not a guarantee that the file
              is issue-free.
            </p>
          )}
          {loaded && !!unanchored.length && (
            <div className="border-t sf-line py-3">
              <h4 className="sf-dim px-4 text-xs">
                Comments without a verified line anchor
              </h4>
              {unanchored.map((f) => (
                <Fragment key={findingKey(f)}>
                  {renderComment(f, false)}
                </Fragment>
              ))}
            </div>
          )}
        </>
      )}
    </section>
  );
});
