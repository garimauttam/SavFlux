/**
 * AgentPanel.test.tsx — the panel against a stream, not against a mock of itself.
 *
 * `apiFetch` is the only thing mocked here; everything between the fetch and the DOM
 * — the chunk loop, the marker scanner, the reducer, the timeline — is the real code.
 * That is the point: the panel used to own its own copy of the parsing loop, and the
 * two bugs this change fixes (a marker split across chunks leaking into the answer,
 * and strict JSON parsing silently dropping every review-shaped marker) were only
 * visible end to end.
 *
 * The marker fixtures below are copied from `backend/app/api/agent.py`'s output. A
 * change to what the server sends is supposed to fail something here.
 */
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { apiFetch } from "../api";
import { AgentPanel } from "./AgentPanel";

vi.mock("../api", () => ({ apiFetch: vi.fn() }));

const fetchMock = apiFetch as unknown as Mock;

const STARTING =
  '{"step": "starting", "message": "Goal: fix TLS", "run_id": "a1b2c3", "mode": "deterministic", ' +
  '"goal": "fix TLS", "max_steps": 8, "plan": "retrieve_context \\u2192 read_file", "plan_steps": [' +
  '{"id": "plan:retrieve_context", "tool": "retrieve_context", "order": 0, "state": "queued"}, ' +
  '{"id": "plan:read_file", "tool": "read_file", "order": 1, "state": "queued"}]}';
const REASONING =
  '{"step": "reasoning", "kind": "plan", "message": "No change was asked for, so the plan stays read-only."}';
const OPEN =
  '{"step": "tool", "tool": "retrieve_context", "step_id": "retrieve_context#1", ' +
  '"plan_id": "plan:retrieve_context", "args": {"query": "TLS verify"}, "message": "retrieve_context: TLS verify"}';
const DONE =
  '{"step": "tool_done", "tool": "retrieve_context", "step_id": "retrieve_context#1", "ok": true, ' +
  '"elapsed_ms": 41, "count": 2, "preview": ["net.py \\u00b7 154 chars"], "preview_more": 0, ' +
  '"plan_id": "plan:retrieve_context", "message": "retrieve_context: 2 chunks"}';
const READ_OPEN =
  '{"step": "tool", "tool": "read_file", "step_id": "read_file#1", "plan_id": "plan:read_file", ' +
  '"args": {"path": "app/net.py"}, "message": "read_file: app/net.py"}';
const COMPLETE =
  '{"step": "complete", "message": "Agent run finished", "steps_used": 2, "max_steps": 8, ' +
  '"elapsed_ms": 132, "truncated": false, "done": 1, "failed": 0, "skipped": 0, "ok": true}';

const REPORT =
  "\n## Report\n\n### Summary\nNo hardcoded secrets found.\n\n```diff\ndiff --git a/net.py b/net.py\n" +
  "@@ -1 +1 @@\n-verify=False\n+verify=True\n```\n";

const status = (payload: string) => `__STATUS__${payload}__STATUS_END__\n`;

/** Full run: everything arrives, then the stream ends. */
const RUN_CHUNKS = [
  status(STARTING), status(REASONING), status(OPEN), status(DONE), REPORT, status(COMPLETE),
];

interface Call { url: string; init: any }

function mockRun(chunks: string[], opts: { hang?: boolean; status?: number; body?: string } = {}): Call[] {
  const calls: Call[] = [];
  fetchMock.mockImplementation(async (url: string, init: any = {}) => {
    calls.push({ url, init });
    if (!url.includes("/agent/run")) return { ok: true, json: async () => ({ tools: [] }) };
    if (opts.status) return { ok: false, status: opts.status, text: async () => opts.body ?? "" };

    let index = 0;
    return {
      ok: true,
      body: {
        getReader: () => ({
          read: async () => {
            if (index < chunks.length) return { done: false, value: new TextEncoder().encode(chunks[index++]) };
            if (opts.hang) {
              await new Promise<never>((_resolve, reject) => {
                init.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")));
              });
            }
            return { done: true, value: undefined };
          },
          cancel: () => Promise.resolve(),
        }),
      },
    };
  });
  return calls;
}

function start(goal = "fix the unsafe TLS verification in net.py") {
  const box = screen.getByRole("textbox", { name: "Agent goal" });
  fireEvent.change(box, { target: { value: goal } });
  fireEvent.click(screen.getByRole("button", { name: /^Run$/ }));
  return box;
}

beforeEach(() => {
  // The header's live clock is a real interval; a component test that let it tick
  // would race with its own assertions for no benefit.
  vi.spyOn(window, "setInterval").mockReturnValue(0 as unknown as NodeJS.Timeout);
  vi.spyOn(window, "clearInterval").mockImplementation(() => {});
});

afterEach(() => {
  vi.restoreAllMocks();
  fetchMock.mockReset();
});

describe("a run, start to finish", () => {
  it("sends the goal with the step budget and no digest", async () => {
    mockRun(RUN_CHUNKS);
    render(<AgentPanel />);
    start();

    await waitFor(() => expect(screen.getByText(/1 of 2 steps done/)).toBeInTheDocument());
    const call = fetchMock.mock.calls.find(([url]) => String(url).includes("/agent/run"));
    const body = JSON.parse(call![1].body);

    expect(call![0]).toBe("/api/v1/agent/run");
    expect(body).toEqual({ goal: "fix the unsafe TLS verification in net.py", max_steps: 8, confirm_digest: null });
    expect(call![1].signal).toBeInstanceOf(AbortSignal);
    expect(call![1].method).toBe("POST");
  });

  it("renders the plan, the reasoning, the cards and the report", async () => {
    mockRun(RUN_CHUNKS);
    render(<AgentPanel />);
    start();

    await waitFor(() => expect(screen.getByText(/1 of 2 steps done/)).toBeInTheDocument());

    // Header: state + server-reported totals, and the mode the run actually used.
    expect(screen.getByText("$0 · deterministic · deterministic")).toBeInTheDocument();
    expect(screen.getByTitle("Time the run reported for itself")).toHaveTextContent("132 ms");
    // 50, not 100: one planned step was never resolved. A run that ends before its
    // plan is exhausted must not look fully done — that is what `truncated` is for.
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "50");

    // Plan strip: both steps, in plan order, resolved.
    const plan = screen.getByRole("list", { name: "Agent plan" });
    expect(plan).toHaveTextContent("Searched the index");
    expect(plan).toHaveTextContent("Read a file");

    // Transcript: the reasoning line sits before the card, as the stream sent it.
    const transcript = screen.getByRole("list", { name: "Agent transcript" });
    const rows = Array.from(transcript.querySelectorAll(":scope > li"));
    expect(rows[0]).toHaveTextContent("No change was asked for");
    expect(rows[1]).toHaveTextContent("41 ms");

    // Report prose is markdown, and the diff is a diff — not a Python block.
    expect(await screen.findByRole("heading", { name: "Report", level: 2 })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Copy patch" })).toBeInTheDocument();
    expect(screen.getByText("+verify=True")).toBeInTheDocument();
  });

  it("marks the run complete without a fake elapsed number while idle", async () => {
    mockRun(RUN_CHUNKS);
    render(<AgentPanel />);
    start();

    await waitFor(() => expect(screen.getByText(/1 of 2 steps done/)).toBeInTheDocument());
    expect(screen.queryByTitle("Wall time since you pressed Run")).not.toBeInTheDocument();
    expect(screen.getByText("2/8 steps")).toBeInTheDocument();
  });
});

describe("the wire", () => {
  it("never shows a marker, however it is split across chunks", async () => {
    // Every byte boundary the run can arrive at, including inside `__STATUS_END__`.
    const whole = RUN_CHUNKS.join("");
    const pieces: string[] = [];
    for (let i = 0; i < whole.length; i += 7) pieces.push(whole.slice(i, i + 7));

    mockRun(pieces);
    render(<AgentPanel />);
    start();

    await waitFor(() => expect(screen.getByRole("button", { name: "Copy patch" })).toBeInTheDocument());
    expect(document.body.textContent).not.toMatch(/__STATUS|__ERROR|step_id|elapsed_ms/);
    expect(document.body.textContent).toContain("No hardcoded secrets found.");
  });

  it("surfaces an error marker as a failed run, with the server's sentence", async () => {
    mockRun([status(STARTING), "__ERROR__chroma is unavailable__ERROR_END__\n"]);
    render(<AgentPanel />);
    start();

    await waitFor(() => expect(screen.getByText("chroma is unavailable")).toBeInTheDocument());
    expect(screen.getAllByText(/failed/).length).toBeGreaterThan(0);
  });

  it("explains a rejected key instead of showing the response body", async () => {
    mockRun([], { status: 401, body: '{"detail":"invalid api key"}' });
    render(<AgentPanel />);
    start();

    await waitFor(() => expect(screen.getByText(/API key was rejected/)).toBeInTheDocument());
    expect(screen.getAllByText(/failed/).length).toBeGreaterThan(0);
  });

  it("counts a rate limit down to the actual limit", async () => {
    mockRun([], { status: 429 });
    render(<AgentPanel />);
    start();

    await waitFor(() => expect(screen.getByText(/10 runs a minute/)).toBeInTheDocument());
  });
});

describe("stop", () => {
  it("aborts the request and reports where the run stopped", async () => {
    const calls = mockRun([status(STARTING), status(OPEN), status(DONE), status(READ_OPEN)], { hang: true });
    render(<AgentPanel />);
    start();

    // In flight: the second step is open, and the composer offers Stop instead of Run.
    await waitFor(() => expect(screen.getByRole("button", { name: /^Stop$/ })).toBeInTheDocument());
    expect(screen.getByRole("textbox", { name: "Agent goal" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: /^Run$/ })).not.toBeInTheDocument();
    expect(screen.getAllByText("running").length).toBeGreaterThan(0);

    fireEvent.click(screen.getByRole("button", { name: /^Stop$/ }));

    await waitFor(() => expect(screen.getByText(/Stopped after 1 of 2 steps/)).toBeInTheDocument());
    const runCall = calls.find((call) => call.url.includes("/agent/run"))!;
    expect(runCall.init.signal.aborted).toBe(true);
    // The step that never finished is shown as interrupted, not as if it succeeded.
    const card = screen.getByRole("button", { name: /read_file: app\/net.py/ });
    expect(card).not.toHaveTextContent("running");
    expect(screen.getByRole("list", { name: "Agent transcript" })).toHaveTextContent("Stopped — the run was interrupted.");
    // And the panel is runnable again.
    expect(screen.getByRole("button", { name: /^Run$/ })).toBeEnabled();
  });

  it("does not start a second run while one is in flight", async () => {
    mockRun([status(STARTING)], { hang: true });
    render(<AgentPanel />);
    const box = start();

    fireEvent.keyDown(box, { key: "Enter" });
    fireEvent.keyDown(box, { key: "Enter" });
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());

    expect(fetchMock.mock.calls.filter(([url]) => String(url).includes("/agent/run"))).toHaveLength(1);
  });
});

describe("the pull-request confirmation", () => {
  const PR_DONE =
    '{"step": "tool_done", "tool": "create_pr", "step_id": "create_pr#1", "ok": true, "elapsed_ms": 6, ' +
    '"status": "manual", "digest": "9f2c1a", "head": "savflux/fix-tls", "title": "fix TLS verification", ' +
    '"gh_command": "gh pr create --repo o/r --head savflux/fix-tls", "message": "create_pr: needs confirmation"}';

  it("appears only once the diff has landed, and re-runs with the digest", async () => {
    mockRun([
      status(STARTING), status(PR_DONE), REPORT,
      status('{"step": "complete", "message": "done", "elapsed_ms": 60}'),
    ]);
    render(<AgentPanel />);
    start("find hardcoded secrets and open a PR with the patch");

    const confirm = await screen.findByRole("button", { name: /I have reviewed the diff/ });
    expect(confirm).toBeInTheDocument();
    // The digest is shown so a reader can tie the confirmation to this exact patch.
    expect(screen.getByText("9f2c1a")).toBeInTheDocument();

    const runCalls = fetchMock.mock.calls.filter(([url]) => String(url).includes("/agent/run"));
    expect(runCalls).toHaveLength(1);

    fireEvent.click(confirm);

    await waitFor(() =>
      expect(fetchMock.mock.calls.filter(([url]) => String(url).includes("/agent/run"))).toHaveLength(2),
    );
    const second = fetchMock.mock.calls.filter(([url]) => String(url).includes("/agent/run"))[1];
    expect(JSON.parse(second![1].body).confirm_digest).toBe("9f2c1a");
    expect(JSON.parse(second![1].body).goal).toBe("find hardcoded secrets and open a PR with the patch");
  });

  it("does not offer a confirmation while the run is still writing the patch", async () => {
    mockRun([status(STARTING), status(PR_DONE)], { hang: true });
    render(<AgentPanel />);
    start();

    await waitFor(() => expect(screen.getByRole("button", { name: /^Stop$/ })).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: /I have reviewed the diff/ })).not.toBeInTheDocument();
  });
});

describe("before a run", () => {
  it("offers goals that are known to work, and fills the composer with one", () => {
    mockRun(RUN_CHUNKS);
    render(<AgentPanel />);

    const chip = screen.getByRole("button", { name: "map how the review budget decides which files a model sees" });
    expect(screen.getByRole("button", { name: /^Run$/ })).toBeDisabled();

    fireEvent.click(chip);
    expect(screen.getByRole("textbox", { name: "Agent goal" })).toHaveValue(
      "map how the review budget decides which files a model sees",
    );
    expect(screen.getByRole("button", { name: /^Run$/ })).toBeEnabled();
  });
});
