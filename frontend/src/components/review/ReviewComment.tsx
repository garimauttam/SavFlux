import { Check, MessageSquare } from "lucide-react";
import type { ReviewFinding } from "../../lib/reviewFindings";
import { SuggestedFix } from "./SuggestedFix";

export function ReviewComment({
  finding,
  resolved,
  onResolve,
  source,
  content,
  hash,
  language,
  verified,
  reviewActive = false,
  onFocus,
}: {
  finding: ReviewFinding;
  resolved: boolean;
  onResolve: () => void;
  source: string;
  content: string;
  hash: string;
  language: string;
  verified: boolean;
  reviewActive?: boolean;
  onFocus: () => void;
}) {
  return (
    <article
      aria-label={`${finding.title} at line ${finding.line}`}
      onMouseEnter={onFocus}
      onFocusCapture={onFocus}
      className="sf-review-comment mx-3 my-3 overflow-hidden rounded-lg text-sm"
    >
      <div className="sf-raised flex flex-wrap items-center gap-2 border-b sf-line px-4 py-2 text-xs">
        <span className="sf-accent rounded-full sf-accent-soft p-1">
          <MessageSquare size={12} />
        </span>
        <strong className="sf-text">SavFlux</strong>
        <span className="sf-mute">
          {finding.origin === "model"
            ? "AI reviewer · verify suggestion"
            : "Static analyzer"}
        </span>
        <span
          className="sf-chip ml-auto"
          style={{
            color: ["high", "critical"].includes(finding.severity)
              ? "var(--sf-bad)"
              : "var(--sf-warn)",
          }}
        >
          {finding.severity}
        </span>
        <span className="sf-mute">
          L{finding.line}
          {finding.endLine && finding.endLine !== finding.line
            ? `–${finding.endLine}`
            : ""}
        </span>
      </div>
      <div className="space-y-3 px-4 py-3">
        <h3
          className={`sf-text font-semibold ${resolved ? "line-through opacity-60" : ""}`}
        >
          {finding.title}
        </h3>
        {!resolved && (
          <>
            <div>
              <p className="sf-mute mb-1 text-[10px] font-semibold uppercase tracking-wider">
                Why this matters
              </p>
              <p className="sf-dim whitespace-pre-wrap break-words leading-relaxed">
                {finding.message}
              </p>
            </div>
            {finding.remediation && (
              <div className="sf-raised rounded-md border sf-line p-3">
                <p className="sf-accent mb-1 text-[10px] font-semibold uppercase tracking-wider">
                  Recommendation
                </p>
                <p className="sf-text whitespace-pre-wrap break-words leading-relaxed">
                  {finding.remediation}
                </p>
              </div>
            )}
            <SuggestedFix
              finding={finding}
              source={source}
              content={content}
              hash={hash}
              language={language}
              verified={verified}
              reviewActive={reviewActive}
            />
          </>
        )}
        <div className="flex items-center justify-between gap-2 border-t sf-line pt-2 text-xs">
          <span className="sf-mute">
            {finding.origin === "static"
              ? `${finding.ruleId} · ${Math.round(finding.confidence * 100)}% analyzer confidence`
              : verified
                ? "Source location verified · suggestion not executed"
                : "Unverified source location · suggestion not executed"}
          </span>
          <button onClick={onResolve} className="sf-btn sf-btn-ghost text-xs">
            {resolved && <Check size={12} />}{" "}
            {resolved ? "Reopen" : "Mark reviewed"}
          </button>
        </div>
      </div>
    </article>
  );
}
