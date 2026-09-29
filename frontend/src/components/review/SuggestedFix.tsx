import { useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import {
  Check,
  Code2,
  Copy,
  Loader2,
  Maximize2,
  Sparkles,
  X,
} from "lucide-react";
import { apiFetch } from "../../api";
import type { ReviewFinding } from "../../lib/reviewFindings";
import { SuggestionDiff } from "./SuggestionDiff";

export interface CodeProposal {
  line: number;
  end_line: number;
  original: string;
  replacement: string;
  explanation?: string;
}
export function checkedProposal(
  data: unknown,
  content: string,
  hash: string,
  finding: ReviewFinding,
): CodeProposal {
  const p = data as Record<string, unknown> | null;
  if (
    !p ||
    p.content_sha256 !== hash ||
    p.applied !== false ||
    !Number.isInteger(p.line) ||
    !Number.isInteger(p.end_line) ||
    (p.line as number) < 1 ||
    (p.line as number) > finding.line ||
    (p.end_line as number) < (finding.endLine ?? finding.line) ||
    (p.end_line as number) > content.split("\n").length ||
    typeof p.original !== "string" ||
    typeof p.replacement !== "string" ||
    p.original !==
      content
        .split("\n")
        .slice((p.line as number) - 1, p.end_line as number)
        .join("\n")
  ) {
    throw new Error(
      "The suggested edit did not match the reviewed source. Nothing was applied.",
    );
  }
  return {
    line: p.line as number,
    end_line: p.end_line as number,
    original: p.original,
    replacement: p.replacement,
    explanation: typeof p.explanation === "string" ? p.explanation : undefined,
  };
}

export function SuggestedFix({
  finding,
  source,
  content,
  hash,
  language,
  verified,
  reviewActive = false,
}: {
  finding: ReviewFinding;
  source: string;
  content: string;
  hash: string;
  language: string;
  verified: boolean;
  reviewActive?: boolean;
}) {
  const id = useId();
  const [proposal, setProposal] = useState<CodeProposal | null>(() =>
    verified && finding.replacement !== undefined
      ? {
          line: finding.line,
          end_line: finding.endLine ?? finding.line,
          original: content
            .split("\n")
            .slice(finding.line - 1, finding.endLine ?? finding.line)
            .join("\n"),
          replacement: finding.replacement,
        }
      : null,
  );
  const [open, setOpen] = useState(Boolean(proposal));
  const [wide, setWide] = useState(false);
  const [split, setSplit] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [copyStatus, setCopyStatus] = useState("");
  const requestRef = useRef<AbortController | null>(null);
  const toggleRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  useEffect(() => () => requestRef.current?.abort(), []);
  // A new batch can start while a suggestion is in flight. Cancel it rather
  // than competing with the review, and discard any late provider response.
  useEffect(() => {
    if (reviewActive) {
      requestRef.current?.abort();
      setBusy(false);
    }
  }, [reviewActive]);
  useEffect(() => {
    if (!wide) return;
    const previous = document.activeElement as HTMLElement | null;
    const dialog = dialogRef.current;
    const oldOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const app = document.getElementById("root");
    const wasInert = app?.inert;
    if (app) app.inert = true;
    const focusable = () =>
      Array.from(
        dialog?.querySelectorAll<HTMLElement>(
          'button:not(:disabled), [tabindex="0"]',
        ) ?? [],
      );
    focusable()[0]?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        setWide(false);
      }
      if (e.key === "Tab") {
        const nodes = focusable(),
          first = nodes[0],
          last = nodes[nodes.length - 1];
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault();
          last?.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first?.focus();
        }
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = oldOverflow;
      if (app) app.inert = wasInert ?? false;
      (previous?.isConnected ? previous : toggleRef.current)?.focus();
    };
  }, [wide]);
  const cancel = () => {
    requestRef.current?.abort();
    setBusy(false);
  };
  const generate = async () => {
    if (!verified || busy || reviewActive) return;
    const controller = new AbortController();
    requestRef.current = controller;
    setBusy(true);
    setError("");
    setCopyStatus("");
    try {
      const response = await apiFetch("/api/v1/review/suggestion", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        signal: controller.signal,
        body: JSON.stringify({
          source,
          content_sha256: hash,
          line: finding.line,
          end_line: finding.endLine ?? finding.line,
          title: finding.title,
          reason: finding.message,
          remediation: finding.remediation,
        }),
      });
      // A proxy timeout may return HTML rather than the API's JSON error.
      if (response.status === 504)
        throw new Error(
          "The review model timed out. No changes were made. Let the repository review finish or press Stop, then retry one fix. If it still times out, choose a smaller local Review model or a configured free-tier provider in Profile settings.",
        );
      const data = await response.json();
      if (!response.ok)
        throw new Error(
          typeof data.detail === "string"
            ? data.detail
            : `Could not generate a fix (HTTP ${response.status}).`,
        );
      const next = checkedProposal(data, content, hash, finding);
      if (!controller.signal.aborted) setProposal(next);
    } catch (e) {
      if (!controller.signal.aborted)
        setError(
          e instanceof Error ? e.message : "Could not generate a code fix.",
        );
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  };
  const copy = async () => {
    if (!proposal) return;
    try {
      await navigator.clipboard.writeText(proposal.replacement);
      setCopyStatus("Copied replacement");
    } catch {
      setCopyStatus(
        "Clipboard unavailable. Select and copy from the After view.",
      );
      setSplit(true);
    }
  };
  const viewer = (
    <>
      <div className="sf-fix-toolbar">
        <span className="sf-dim text-xs">
          {proposal
            ? `Proposed edit · L${proposal.line}–${proposal.end_line}`
            : "Code suggestion"}
        </span>
        {proposal && (
          <>
            <div className="ml-auto flex gap-1">
              <button
                aria-pressed={!split}
                className="sf-btn sf-btn-ghost"
                onClick={() => setSplit(false)}
              >
                Unified
              </button>
              <button
                aria-pressed={split}
                className="sf-btn sf-btn-ghost"
                onClick={() => setSplit(true)}
              >
                Before / after
              </button>
            </div>
            <button className="sf-btn sf-btn-secondary" onClick={copy}>
              <Copy size={13} /> Copy replacement
            </button>
          </>
        )}
        {!wide && (
          <button
            className="sf-iconbtn ml-auto h-7 w-7"
            aria-label="Expand suggested fix"
            title="Open larger code comparison"
            onClick={() => setWide(true)}
          >
            <Maximize2 size={14} />
          </button>
        )}
      </div>
      {proposal ? (
        <>
          <div
            className="sf-fix-code"
            tabIndex={0}
            aria-label="Code comparison"
          >
            <SuggestionDiff
              {...proposal}
              startLine={proposal.line}
              language={language}
              split={split}
            />
          </div>
          <div className="sf-dim space-y-1 border-t sf-line p-3 text-xs">
            <p>
              {proposal.explanation ||
                "Use this replacement for the indicated source range."}
            </p>
            <p>
              Not applied or executed. Source location checked; review and test
              the proposed behavior.
            </p>
          </div>
        </>
      ) : (
        <div className="sf-dim space-y-3 p-4 text-xs">
          <p>
            {verified
              ? "This finding has a recommendation, but no concrete code edit yet."
              : "Code fixes are disabled because this source could not be matched to the review. Re-index and run a fresh review."}
          </p>
          {verified && (
            <>
              <p>
                Generate a focused before/after proposal using your configured
                review model. Cloud providers may charge for this request. Your
                files will not be changed.
              </p>
              {(finding.endLine ?? finding.line) - finding.line + 1 > 100 && (
                <p>
                  This finding spans{" "}
                  {(finding.endLine ?? finding.line) - finding.line + 1} lines.
                  A large refactor may exceed the model’s time or output limit.
                </p>
              )}
              {reviewActive && (
                <p id={`${id}-waiting`} role="status">
                  Code fix generation is paused while this repository review is
                  running, to avoid competing model requests. Let it finish or
                  press Stop, then generate a fix. Existing suggestions remain
                  viewable.
                </p>
              )}
              <div className="flex flex-wrap gap-2">
                <button
                  className="sf-btn sf-btn-primary"
                  disabled={busy || reviewActive}
                  aria-describedby={reviewActive ? `${id}-waiting` : undefined}
                  onClick={() => void generate()}
                >
                  {busy ? (
                    <Loader2 size={14} className="animate-spin" />
                  ) : (
                    <Sparkles size={14} />
                  )}{" "}
                  {busy ? "Generating code…" : "Generate code fix"}
                </button>
                {busy && (
                  <button className="sf-btn sf-btn-secondary" onClick={cancel}>
                    Cancel
                  </button>
                )}
              </div>
            </>
          )}
        </div>
      )}
      {error && (
        <p role="alert" className="p-3 text-xs text-red-400">
          {error}
        </p>
      )}
      {copyStatus && (
        <p role="status" className="sf-dim flex items-center gap-1 p-3 text-xs">
          <Check size={12} />
          {copyStatus}
        </p>
      )}
    </>
  );
  return (
    <div className="sf-fix-container">
      <button
        ref={toggleRef}
        className="sf-fix-toggle"
        aria-expanded={open}
        aria-controls={id}
        onClick={() => {
          if (open) cancel();
          setOpen(!open);
        }}
      >
        <Code2 size={15} />
        <span>Suggested fix</span>
        <span className="sf-mute ml-auto text-[11px]">
          {open
            ? "Collapse"
            : proposal
              ? "View code changes"
              : "Open code suggestion"}
        </span>
      </button>
      <div
        id={id}
        className={`sf-fix-expand ${open ? "is-open" : ""}`}
        aria-hidden={!open}
      >
        <div className="min-h-0 overflow-hidden">{open && !wide && viewer}</div>
      </div>
      {wide &&
        createPortal(
          <div
            className="sf-fix-backdrop"
            onMouseDown={(e) => {
              if (e.target === e.currentTarget) setWide(false);
            }}
          >
            <div
              ref={dialogRef}
              role="dialog"
              aria-modal="true"
              aria-labelledby={`${id}-title`}
              className="sf-fix-dialog"
            >
              <div className="sf-fix-toolbar">
                <div className="min-w-0">
                  <h2
                    id={`${id}-title`}
                    className="sf-text truncate text-sm font-semibold"
                  >
                    Suggested fix · {finding.title}
                  </h2>
                  <p className="sf-mute truncate text-xs">
                    {source.split("::").pop()}
                  </p>
                </div>
                <button
                  autoFocus
                  aria-label="Close suggested fix"
                  className="sf-iconbtn ml-auto h-8 w-8"
                  onClick={() => setWide(false)}
                >
                  <X size={18} />
                </button>
              </div>
              {viewer}
            </div>
          </div>,
          document.body,
        )}
    </div>
  );
}
