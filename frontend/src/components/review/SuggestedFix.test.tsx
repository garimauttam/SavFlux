import { StrictMode } from "react";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { apiFetch } from "../../api";
import { SuggestedFix, checkedProposal } from "./SuggestedFix";
import { SuggestionDiff } from "./SuggestionDiff";
import type { ReviewFinding } from "../../lib/reviewFindings";
vi.mock("../../api", () => ({ apiFetch: vi.fn() }));
const finding: ReviewFinding = {
  ruleId: "tls",
  title: "Verify TLS",
  line: 2,
  severity: "high",
  message: "Use TLS",
  remediation: "Remove false",
  evidence: "",
  origin: "static",
  confidence: 1,
};
const content = "import requests\nrequests.get(url, verify=False)\n";
const hash = "a".repeat(64);
const response = {
  line: 2,
  end_line: 2,
  original: "requests.get(url, verify=False)",
  replacement: "requests.get(url)",
  content_sha256: hash,
  applied: false,
};
const view = (verified = true, replacement?: string) =>
  render(
    <SuggestedFix
      finding={{ ...finding, replacement }}
      content={content}
      source="repo::auth.py"
      hash={hash}
      language="python"
      verified={verified}
    />,
  );
beforeEach(() => {
  vi.mocked(apiFetch).mockReset();
});
afterEach(() => vi.restoreAllMocks());
it("only invokes the model after explicit consent; shows diff, copies and expands with focus return", async () => {
  vi.mocked(apiFetch).mockResolvedValue(new Response(JSON.stringify(response)));
  const copy = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", {
    value: { writeText: copy },
    configurable: true,
  });
  view();
  fireEvent.click(screen.getByRole("button", { name: /Suggested fix/ }));
  expect(apiFetch).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Generate code fix" }));
  await screen.findByLabelText("Suggested code diff");
  expect(document.querySelector(".is-removed")).toHaveTextContent(
    "verify=False",
  );
  expect(document.querySelector(".is-added")).toHaveTextContent(
    "requests.get(url)",
  );
  fireEvent.click(screen.getByRole("button", { name: "Copy replacement" }));
  await screen.findByText("Copied replacement");
  expect(copy).toHaveBeenCalledWith("requests.get(url)");
  fireEvent.click(screen.getByRole("button", { name: "Expand suggested fix" }));
  expect(screen.getByRole("dialog")).toBeInTheDocument();
  fireEvent.keyDown(document, { key: "Escape" });
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(screen.getByRole("button", { name: /Suggested fix/ })).toHaveFocus();
});
it("does not generate for unverified source", () => {
  view(false);
  fireEvent.click(screen.getByRole("button", { name: /Suggested fix/ }));
  expect(
    screen.queryByRole("button", { name: "Generate code fix" }),
  ).toBeNull();
  expect(screen.getByText(/Code fixes are disabled/)).toBeVisible();
});
it("supports existing code edits, including deletion, without making a provider call", () => {
  view(true, "");
  expect(screen.getByText(/delete the removed lines/)).toBeVisible();
  expect(
    screen.queryByRole("button", { name: "Generate code fix" }),
  ).toBeNull();
});
it("rejects stale hashes, altered originals and out-of-bounds proposals", () => {
  for (const changes of [
    { content_sha256: "x" },
    { original: "wrong" },
    { line: 0 },
    { end_line: 100 },
    { applied: true },
  ]) {
    expect(() =>
      checkedProposal({ ...response, ...changes }, content, hash, finding),
    ).toThrow(/did not match/);
  }
});
it("shows provider failure and allows retry", async () => {
  vi.mocked(apiFetch).mockResolvedValue(
    new Response(JSON.stringify({ detail: "Model unavailable" }), {
      status: 502,
    }),
  );
  view();
  fireEvent.click(screen.getByRole("button", { name: /Suggested fix/ }));
  fireEvent.click(screen.getByRole("button", { name: "Generate code fix" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "Model unavailable",
  );
  expect(
    screen.getByRole("button", { name: "Generate code fix" }),
  ).toBeEnabled();
});
it("cancels provider work on collapse and discards late responses", async () => {
  let finish!: (r: Response) => void;
  vi.mocked(apiFetch).mockImplementation(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  );
  view();
  fireEvent.click(screen.getByRole("button", { name: /Suggested fix/ }));
  fireEvent.click(screen.getByRole("button", { name: "Generate code fix" }));
  const signal = vi.mocked(apiFetch).mock.calls.at(-1)?.[1]?.signal;
  fireEvent.click(screen.getByRole("button", { name: /Suggested fix/ }));
  expect(signal?.aborted).toBe(true);
  finish(new Response(JSON.stringify(response)));
  fireEvent.click(screen.getByRole("button", { name: /Suggested fix/ }));
  await waitFor(() =>
    expect(
      screen.getByRole("button", { name: "Generate code fix" }),
    ).toBeEnabled(),
  );
  expect(screen.queryByLabelText("Suggested code diff")).toBeNull();
});
it("keeps old/new line numbers stable under StrictMode and rerenders", () => {
  const element = (
    <StrictMode>
      <SuggestionDiff
        original={"x = 1\ny = 2"}
        replacement={"x = 1\ny = 3"}
        language="python"
        startLine={7}
      />
    </StrictMode>
  );
  const { container, rerender } = render(element);
  const numbers = () =>
    [...container.querySelectorAll(".sf-line-number")].map(
      (el) => el.textContent,
    );
  expect(numbers()).toEqual(["7", "7", "8", "", "", "8"]);
  rerender(element);
  expect(numbers()).toEqual(["7", "7", "8", "", "", "8"]);
  expect(container.querySelectorAll(".token").length).toBeGreaterThan(0);
});

const guarded = (reviewActive: boolean, replacement?: string) => (
  <SuggestedFix
    finding={{ ...finding, replacement }}
    content={content}
    source="repo::auth.py"
    hash={hash}
    language="python"
    verified
    reviewActive={reviewActive}
  />
);
it("blocks generation during review and enables it after Stop or completion", async () => {
  const { rerender } = render(guarded(true));
  fireEvent.click(screen.getByRole("button", { name: /Suggested fix/ }));
  const button = screen.getByRole("button", { name: "Generate code fix" });
  expect(button).toBeDisabled();
  expect(button).toHaveAccessibleDescription(/press Stop/);
  fireEvent.click(button);
  expect(apiFetch).not.toHaveBeenCalled();
  rerender(guarded(false));
  expect(button).toBeEnabled();
  vi.mocked(apiFetch).mockResolvedValue(new Response(JSON.stringify(response)));
  fireEvent.click(button);
  await screen.findByLabelText("Suggested code diff");
});
it("keeps existing diffs viewable while review is running", () => {
  render(guarded(true, "requests.get(url)"));
  expect(screen.getByLabelText("Suggested code diff")).toBeVisible();
  expect(apiFetch).not.toHaveBeenCalled();
});
it("cancels an in-flight fix when a new review starts, ignoring late results", async () => {
  let finish!: (r: Response) => void;
  vi.mocked(apiFetch).mockImplementation(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  );
  const { rerender } = render(guarded(false));
  fireEvent.click(screen.getByRole("button", { name: /Suggested fix/ }));
  fireEvent.click(screen.getByRole("button", { name: "Generate code fix" }));
  const signal = vi.mocked(apiFetch).mock.calls.at(-1)?.[1]?.signal;
  rerender(guarded(true));
  expect(signal?.aborted).toBe(true);
  await act(async () => {
    finish(new Response(JSON.stringify(response)));
  });
  expect(screen.queryByLabelText("Suggested code diff")).toBeNull();
  rerender(guarded(false));
  expect(
    screen.getByRole("button", { name: "Generate code fix" }),
  ).toBeEnabled();
});
it("turns a timeout into actionable review-model guidance, including with an older backend", async () => {
  vi.mocked(apiFetch).mockResolvedValue(
    new Response(JSON.stringify({ detail: "The model timed out." }), {
      status: 504,
    }),
  );
  view();
  fireEvent.click(screen.getByRole("button", { name: /Suggested fix/ }));
  fireEvent.click(screen.getByRole("button", { name: "Generate code fix" }));
  const error = await screen.findByRole("alert");
  expect(error).toHaveTextContent("No changes were made");
  expect(error).toHaveTextContent("smaller local Review model");
  expect(error).toHaveTextContent("free-tier provider");
});

it("handles a non-JSON gateway timeout without showing a JSON parser error", async () => {
  vi.mocked(apiFetch).mockResolvedValue(
    new Response("<html>Gateway timeout</html>", { status: 504 }),
  );
  view();
  fireEvent.click(screen.getByRole("button", { name: /Suggested fix/ }));
  fireEvent.click(screen.getByRole("button", { name: "Generate code fix" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "The review model timed out",
  );
});
