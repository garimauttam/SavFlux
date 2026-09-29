import { describe, expect, it } from "vitest";
import { codeRows, decodeFinding, reviewPath } from "./reviewFindings";
describe("review annotations", () => {
  it("rejects invalid lines and unknown origins instead of guessing an anchor", () => {
    expect(
      decodeFinding({ origin: "static", line: -1, title: "x", rule_id: "x" }),
    ).toBeNull();
    expect(
      decodeFinding({ origin: "untrusted", line: 1, title: "x", rule_id: "x" }),
    ).toBeNull();
    expect(
      decodeFinding({ origin: "static", line: 1.5, title: "x", rule_id: "x" }),
    ).toBeNull();
  });
  it("preserves model proposals without turning them into applied edits", () => {
    const f = decodeFinding({
      origin: "model",
      line: 10,
      title: "Use safe call",
      rule_id: "model-1",
      replacement: "safe()",
    });
    expect(f?.origin).toBe("model");
    expect(f?.replacement).toBe("safe()");
  });
  it("collapses unannotated code and expands the exact requested range", () => {
    const rows = codeRows(100, [], new Set());
    expect(rows.at(-1)).toEqual({ kind: "gap", start: 61, end: 100 });
    expect(codeRows(100, [], new Set(["61:100"]))).toHaveLength(100);
  });
  it("does not strip directory names or confuse duplicate basenames", () => {
    expect(reviewPath("https://github.com/o/r::src/a.ts")).toBe("src/a.ts");
    expect(reviewPath("https://github.com/o/r::test/a.ts")).toBe("test/a.ts");
  });
});
