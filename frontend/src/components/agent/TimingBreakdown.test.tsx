/**
 * TimingBreakdown.test.tsx — the split must stay readable, and must stay honest.
 *
 * Two properties carry most of the weight: every number the component prints is also
 * printed as text (the bar is decoration, so colour and geometry are not the channel),
 * and a stage nobody measured is absent rather than zero.
 */
import { describe, expect, it } from "vitest";
import { render, screen, within } from "@testing-library/react";
import { TimingBreakdown } from "./TimingBreakdown";
import { EMPTY_TIMINGS, applyTiming } from "../../lib/timings";
import type { ReviewTimings } from "../../lib/timings";

const repoRun: ReviewTimings = [
  { step: "planned", plan_ms: 12, context_ms: 810 },
  { step: "timing", stage: "files", elapsed_ms: 3_050, model_calls: 2, cache_hits: 1,
    static_only: 1, slowest_file: "pkg/net.py", llm_ms: 2_900, wait_ms: 120 },
  { step: "complete", file: "pkg/net.py", llm_ms: 2_900, wait_ms: 120 },
  { step: "complete", file: "pkg/util.py", llm_ms: 0, wait_ms: 4 },
  { step: "complete", file: "pkg/app.py", cached: true, wait_ms: 2 },
  { step: "complete", file: "pkg/cli.py", wait_ms: 1 },
].reduce((state, meta) => applyTiming(state, meta), EMPTY_TIMINGS);

const fastSingle: ReviewTimings = [
  { step: "writing", mode: "fast", analysis_ms: 41, findings: 3 },
  { step: "complete", mode: "fast", elapsed_ms: 1_041, analysis_ms: 41, findings: 3 },
].reduce((state, meta) => applyTiming(state, meta), EMPTY_TIMINGS);

describe("rendering", () => {
  it("prints every stage as a label, a duration and a share", () => {
    render(<TimingBreakdown timings={repoRun} />);

    const rows = within(screen.getByRole("list", { name: "Measured stages" })).getAllByRole("listitem");

    expect(rows).toHaveLength(3);
    // label, what it covers, the number, and the share — in that order, largest first.
    expect(rows[0].textContent).toContain("Reviewing files");
    expect(rows[0].textContent).toContain("2 model call(s)");
    expect(rows[0].textContent).toContain("3.05 s");
    expect(rows[0].textContent).toContain("79%");
    expect(rows[1].textContent).toContain("Cross-file context");
    expect(rows[1].textContent).toContain("810 ms");
    expect(rows[2].textContent).toContain("Plan & triage");
    expect(rows[2].textContent).toContain("0%");
  });

  it("keeps the geometry decorative, so the numbers survive without colour", () => {
    const { container } = render(<TimingBreakdown timings={repoRun} />);
    const bar = container.querySelector(".h-1\\.5");

    expect(bar).toHaveAttribute("aria-hidden", "true");
    expect(screen.getByRole("list", { name: "Measured stages" })).toBeInTheDocument();
  });

  it("shows the slowest file with the phase that made it slow", () => {
    render(<TimingBreakdown timings={repoRun} />);

    expect(screen.getByText(/Slowest file/)).toHaveTextContent("pkg/net.py");
    expect(screen.getByText(/Slowest file/)).toHaveTextContent("model 2.90 s");
    expect(screen.getByText(/Slowest file/)).toHaveTextContent("queued 120 ms");
  });

  it("distinguishes a cached file from an instant one, in the per-file rows", () => {
    render(<TimingBreakdown timings={repoRun} />);

    const rows = screen.getByRole("list", { name: "Slowest files" });
    const text = rows.textContent ?? "";

    expect(text).toContain("pkg/net.py");
    expect(text).toContain("model 2.90 s");
    // 0 ms is a measurement; "no model call" is the absence of one. They must not
    // render the same way, or a cached run looks like an instant model.
    expect(text).toContain("0 ms");
    expect(text).toContain("no model call");
    expect(text).toContain("cache");
  });

  it("shows the split for a single-file review, model time derived by subtraction", () => {
    render(<TimingBreakdown timings={fastSingle} />);

    const rows = within(screen.getByRole("list", { name: "Measured stages" })).getAllByRole("listitem");

    expect(rows[0]).toHaveTextContent("Model");
    expect(rows[0]).toHaveTextContent("1.00 s");
    expect(rows[1]).toHaveTextContent("Static analysis");
    expect(rows[1]).toHaveTextContent("41 ms");
    // The headline is the sum of what was measured, never the browser's own clock —
    // and it says so, because "1.04 s" alone reads as the server's total for a
    // single-file run that also spent time in the network.
    const headline = screen.getByText("1.04 s");
    expect(headline.parentElement).toHaveTextContent("measured here");
  });

  it("marks a client-measured clock as a client measurement", () => {
    render(<TimingBreakdown timings={fastSingle} running liveMs={640} />);

    const live = screen.getByText("+640 ms elapsed");
    expect(live.getAttribute("title")).toContain("measured by the browser");
  });

  it("says nothing has finished yet, instead of drawing an empty chart", () => {
    render(<TimingBreakdown timings={EMPTY_TIMINGS} running liveMs={300} />);

    expect(screen.getByText(/No stage has finished yet/)).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "Measured stages" })).not.toBeInTheDocument();
  });

  it("renders nothing at all for a run that never reported timings", () => {
    const { container } = render(<TimingBreakdown timings={EMPTY_TIMINGS} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("hides the per-file list when one file explains everything already", () => {
    const one = applyTiming(EMPTY_TIMINGS, { step: "complete", file: "only.py", llm_ms: 50 });
    render(<TimingBreakdown timings={one} />);

    expect(screen.queryByRole("list", { name: "Slowest files" })).not.toBeInTheDocument();
  });
});
