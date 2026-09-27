/**
 * AgentTimeline.test.tsx — the transcript, rendered.
 *
 * What these assert is the information architecture, not pixels: a collapsed row must
 * answer "did this go well, and what did it cost", and expanding it must answer "what
 * did it ask for, and what came back". A test that only checked that text appeared
 * would have passed on the flat bullet list this replaced, so every assertion is about
 * *where* a piece of a marker lands.
 */
import { describe, expect, it } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { AgentTimeline, PlanStrip, ToolCardRow, toolLabel } from "./AgentTimeline";
import type { PlanRow, ToolCard } from "../../lib/agentRun";
import { reduceAll, resetIds } from "../../lib/agentRun";
import type { StreamEvent } from "../../lib/stream";

function card(over: Partial<ToolCard> = {}): ToolCard {
  return {
    stepId: "read_file#1",
    planId: "plan:read_file",
    tool: "read_file",
    state: "done",
    message: "read_file: 4,400 chars",
    args: { source: "backend/app/services/reranker.py" },
    preview: ["reranker.py · 4400 chars"],
    previewMore: 0,
    elapsedMs: 121,
    fields: { chars: 4400 },
    ...over,
  };
}

const row = (over: Partial<PlanRow> = {}): PlanRow => ({
  id: "plan:read_file", tool: "read_file", order: 0, state: "queued", cards: 0, runs: 0, ...over,
});

describe("toolLabel", () => {
  it("turns a tool name into a verb phrase, and keeps an unknown one readable", () => {
    expect(toolLabel("retrieve_context")).toBe("Searched the index");
    expect(toolLabel("brand_new")).toBe("brand new");
  });
});

describe("ToolCardRow", () => {
  it("collapses to one line that carries state, duration and the message", () => {
    render(<ToolCardRow card={card()} index={2} />);

    const line = screen.getByRole("button");
    expect(line).toHaveTextContent("Read a file");
    expect(line).toHaveTextContent("read_file: 4,400 chars");
    expect(line).toHaveTextContent("121 ms");
    expect(line).toHaveAttribute("aria-expanded", "false");
    // The index is what lets a reader say "step 2" out loud.
    expect(within(line).getByText("2")).toBeInTheDocument();
  });

  it("expands to arguments and results", () => {
    render(<ToolCardRow card={card()} index={1} />);
    fireEvent.click(screen.getByRole("button"));

    expect(screen.getByRole("button")).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("Arguments")).toBeInTheDocument();
    expect(screen.getByTitle("source = backend/app/services/reranker.py")).toBeInTheDocument();
    expect(screen.getByText("Found")).toBeInTheDocument();
    expect(screen.getByText("reranker.py · 4400 chars")).toBeInTheDocument();
    expect(screen.getByText("4400")).toBeInTheDocument();
  });

  it("hides plumbing fields from the result grid but shows the rest", () => {
    render(<ToolCardRow card={card({ fields: { file: "a.py", tier: "fast", digest: "abc" } })} index={1} />);
    fireEvent.click(screen.getByRole("button"));

    expect(screen.getByText("abc")).toBeInTheDocument();
    expect(screen.queryByText("a.py")).not.toBeInTheDocument();
    expect(screen.queryByText("fast")).not.toBeInTheDocument();
  });

  it("says a step ran with no arguments rather than rendering an empty gap", () => {
    render(<ToolCardRow card={card({ args: {} })} index={1} />);
    fireEvent.click(screen.getByRole("button"));

    expect(screen.getByText("no arguments")).toBeInTheDocument();
  });

  it("counts the results it did not list", () => {
    render(<ToolCardRow card={card({ previewMore: 44 })} index={1} />);
    fireEvent.click(screen.getByRole("button"));

    expect(screen.getByText("+44 more")).toBeInTheDocument();
  });

  it("shows a running step as running, with no invented duration", () => {
    render(<ToolCardRow card={card({ state: "running", elapsedMs: null, preview: [], fields: {} })} index={1} />);

    const line = screen.getByRole("button");
    expect(line).toHaveTextContent("running");
    expect(line).not.toHaveTextContent(/ms/);
  });

  it("keeps a failure visible without expanding", () => {
    render(<ToolCardRow card={card({ state: "failed", message: "autofix failed: chroma down" })} index={1} />);

    expect(screen.getByRole("button")).toHaveTextContent("autofix failed: chroma down");
  });

  it("links a created pull request from the card that created it", () => {
    render(<ToolCardRow card={card({
      tool: "create_pr", fields: { url: "https://github.com/o/r/pull/9", status: "created" },
    })} index={1} />);
    fireEvent.click(screen.getByRole("button"));

    const link = screen.getByRole("link", { name: /open pull request/i });
    expect(link).toHaveAttribute("href", "https://github.com/o/r/pull/9");
    expect(link).toHaveAttribute("rel", "noreferrer");
  });

  it("offers the gh command only once, in the expanded body", () => {
    render(<ToolCardRow card={card({
      tool: "create_pr",
      fields: { status: "manual", gh_command: "gh pr create --repo o/r --head savflux/x" },
    })} index={1} />);

    expect(screen.queryByText(/^gh pr create/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button"));
    expect(screen.getAllByText(/^gh pr create/)).toHaveLength(1);
  });

  it("gives the disclosure a region id the panel can address", () => {
    render(<ToolCardRow card={card({ stepId: "build_patch#1" })} index={1} />);
    const button = screen.getByRole("button");

    expect(button).toHaveAttribute("aria-controls", "agent-step-build_patch-1");
    expect(document.getElementById("agent-step-build_patch-1")).toBeNull();

    fireEvent.click(button);
    expect(document.getElementById("agent-step-build_patch-1")).not.toBeNull();

    fireEvent.click(button);
    expect(document.getElementById("agent-step-build_patch-1")).toBeNull();
  });
});

describe("PlanStrip", () => {
  it("lists every planned step in order, with its state", () => {
    render(<PlanStrip rows={[
      row({ tool: "retrieve_context", state: "done" }),
      row({ id: "plan:autofix", tool: "autofix", order: 1, state: "running", cards: 3, runs: 2 }),
    ]} />);

    const list = screen.getByRole("list", { name: "Agent plan" });
    const items = within(list).getAllByRole("listitem").slice(1); // [0] is the "Plan" label

    expect(items.map((item) => item.textContent)).toEqual(["Searched the index→", "Applied verified fixes ×2"]);
    expect(items[0]).toHaveTextContent("Searched the index");
    expect(within(items[1]).getByTitle("autofix — running")).toBeInTheDocument();
    // A step that ran three times is still one plan row; the count is the difference.
    expect(within(items[1]).getByText("×2")).toBeInTheDocument();
  });

  it("renders nothing before a plan exists", () => {
    const { container } = render(<PlanStrip rows={[]} />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe("AgentTimeline", () => {
  const events: StreamEvent[] = [
    { step: "starting", message: "Goal", run_id: "r", max_steps: 8, goal: "g",
      plan: "read_file → autofix",
      plan_steps: [{ id: "plan:read_file", tool: "read_file", order: 0, state: "queued" },
                   { id: "plan:autofix", tool: "autofix", order: 1, state: "queued" }] },
    { step: "reasoning", kind: "plan", message: "No change was asked for, so the plan stays read-only." },
    { step: "tool", message: "read_file: a.py", tool: "read_file", step_id: "read_file#1",
      plan_id: "plan:read_file", args: { source: "a.py" } },
    { step: "tool_done", message: "read_file: 12 chars", tool: "read_file", step_id: "read_file#1",
      ok: true, elapsed_ms: 4, preview: ["a.py · 12 chars"], preview_more: 0, plan_id: "plan:read_file" },
    { step: "tool_skipped", message: "autofix: nothing to repair", tool: "autofix", plan_id: "plan:autofix" },
    { step: "notice_probe", message: "A marker from a newer server" },
  ];

  it("renders the transcript in stream order: thought, card, skip, notice", () => {
    resetIds();
    const state = reduceAll(events);
    const { container } = render(<AgentTimeline items={state.items} cards={state.cards} />);

    const rows = Array.from(container.querySelectorAll("ol[aria-label='Agent transcript'] > li"));
    expect(rows.map((r) => r.textContent)).toEqual([
      expect.stringContaining("No change was asked for"),
      expect.stringContaining("Read a file"),
      expect.stringContaining("nothing to repair"),
      expect.stringContaining("A marker from a newer server"),
    ]);
  });

  it("renders nothing when the run has no transcript yet", () => {
    const { container } = render(<AgentTimeline items={[]} cards={{}} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("does not invent a row for a marker whose card is missing", () => {
    const { container } = render(
      <AgentTimeline items={[{ kind: "card", id: "x", stepId: "ghost#1" }]} cards={{}} />,
    );
    expect(container.querySelectorAll("li")).toHaveLength(0);
  });

  it("collapses a long reasoning line and expands it on demand", () => {
    const long = "Because the goal named a fix, the write path is enabled: autofix, build_patch. ".repeat(6);
    render(<AgentTimeline items={[{ kind: "reasoning", id: "r1", text: long }]} cards={{}} />);

    const disclosure = screen.getByRole("button", { name: /Because the goal named a fix/ });
    expect(disclosure).toHaveAttribute("aria-expanded", "false");
    expect(disclosure).toHaveTextContent("more");

    fireEvent.click(disclosure);
    expect(disclosure).toHaveAttribute("aria-expanded", "true");
    expect(disclosure).toHaveTextContent("less");
  });

  it("leaves a short reasoning line inert rather than a disclosure with nothing to hide", () => {
    render(<AgentTimeline items={[{ kind: "reasoning", id: "r1", text: "Read-only run." }]} cards={{}} />);
    const disclosure = screen.getByRole("button", { name: "Read-only run." });

    expect(disclosure).toBeDisabled();
    expect(disclosure).not.toHaveTextContent("more");
  });
});
