import { useMemo } from "react";
import { diffLines } from "diff";
import { ReviewSyntax } from "../../lib/highlight";

/** Diff colors are backgrounds; syntax colors still describe the source language. */
export function SuggestionDiff({
  original,
  replacement,
  language,
  startLine,
  split = false,
}: {
  original: string;
  replacement: string;
  language: string;
  startLine: number;
  split?: boolean;
}) {
  const changes = useMemo(
    () => diffLines(original, replacement),
    [original, replacement],
  );
  const renderSide = (text: string, side: "before" | "after") => (
    <section
      aria-label={side === "before" ? "Original code" : "Suggested code"}
    >
      <div className="sf-diff-heading">
        {side === "before" ? "Before" : "After · suggested"}
      </div>
      {text === "" ? (
        <p className="sf-mute p-3 text-xs">
          {side === "after" ? "Delete these lines" : "Empty range"}
        </p>
      ) : (
        <ReviewSyntax
          code={text}
          language={language}
          renderLines={(rows) =>
            rows.map((row, i) => (
              <div
                className={`sf-code-line ${side === "before" ? "is-removed" : "is-added"}`}
                key={i}
              >
                <span className="sf-line-number">{startLine + i}</span>
                <span className="sf-diff-sign">
                  {side === "before" ? "−" : "+"}
                </span>
                <code>{row}</code>
              </div>
            ))
          }
        />
      )}
    </section>
  );
  if (split)
    return (
      <div className="sf-split-diff">
        {renderSide(original, "before")}
        {renderSide(replacement, "after")}
      </div>
    );
  // Tokenize each complete side, not isolated lines, so multiline syntax survives.
  return (
    <ReviewSyntax
      code={original}
      language={language}
      renderLines={(oldRows) => (
        <ReviewSyntax
          code={replacement}
          language={language}
          renderLines={(newRows) => {
            let before = startLine,
              after = startLine;
            return (
              <div aria-label="Suggested code diff">
                {changes.flatMap((change, group) => {
                  const count = change.count ?? 0;
                  return Array.from({ length: count }, (_, i) => {
                    const old = change.added ? null : before++;
                    const next = change.removed ? null : after++;
                    return (
                      <div
                        key={`${group}:${i}`}
                        className={`sf-code-line ${change.added ? "is-added" : change.removed ? "is-removed" : ""}`}
                      >
                        <span className="sf-line-number">{old ?? ""}</span>
                        <span className="sf-line-number">{next ?? ""}</span>
                        <span className="sf-diff-sign">
                          {change.added ? "+" : change.removed ? "−" : " "}
                        </span>
                        <code>
                          {change.removed
                            ? oldRows[old! - startLine]
                            : newRows[next! - startLine]}
                        </code>
                      </div>
                    );
                  });
                })}
                {replacement === "" && (
                  <p className="sf-mute p-2 text-xs">
                    Suggested change: delete the removed lines.
                  </p>
                )}
              </div>
            );
          }}
        />
      )}
    />
  );
}
