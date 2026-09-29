/**
 * useMultiReview.test.ts — how a refused review request is described.
 *
 * WHY THIS FILE EXISTS
 * --------------------
 * A 238-file selection was refused and the reader was shown:
 *
 *   "Value error: Too many files. Maximum 200 files per multi-review request."
 *
 * Two defects in one line. "Value error: " is Pydantic's internal context
 * prefix for an exception raised inside a validator — a Python implementation
 * detail, in a product message. And the sentence said what the limit was but
 * not what to do about it, so the page offered no way forward.
 *
 * The cap itself is now 2,000 and configurable; the server-side message is
 * covered in `backend/tests/test_review_request_limits.py`. This file pins the
 * half only the client can be wrong about: whatever the server sends, a
 * Pydantic prefix never reaches the reader.
 */
import { afterEach, describe, expect, it, vi, type Mock } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import { apiFetch } from "../api";
import type { IndexedFile } from "../types";
import { useMultiReview } from "./useMultiReview";

vi.mock("../api", () => ({ apiFetch: vi.fn() }));
const fetchMock = apiFetch as unknown as Mock;

afterEach(() => {
  fetchMock.mockReset();
});

const FILES: IndexedFile[] = [
  {
    source: "https://github.com/o/r::a.py",
    file_name: "a.py",
    language: "py",
  } as IndexedFile,
];

/** A 422 carrying Pydantic's `detail` array shape. */
function refuses(detail: unknown) {
  return {
    ok: false,
    status: 422,
    json: async () => ({ detail }),
  };
}

async function run() {
  const { result } = renderHook(() => useMultiReview());
  await act(async () => {
    await result.current.reviewFiles(FILES);
  });
  await waitFor(() => expect(result.current.error).toBeTruthy());
  return result.current.error as string;
}

describe("useMultiReview — a refused request is described, not dumped", () => {
  it("never shows Pydantic's internal prefix", async () => {
    fetchMock.mockResolvedValue(
      refuses([{ msg: "Value error: At least one file must be provided." }]),
    );

    const message = await run();

    expect(message).not.toContain("Value error");
    expect(message).toBe("At least one file must be provided.");
  });

  it("strips the prefix from every message in a multi-error list", async () => {
    fetchMock.mockResolvedValue(
      refuses([
        { msg: "Value error: first problem" },
        { msg: "Value error: second problem" },
      ]),
    );

    const message = await run();

    expect(message).toBe("first problem; second problem");
  });

  it("keeps a clean server message exactly as written", async () => {
    const sent =
      "2001 files selected, but one review request carries at most 2000. " +
      "Deselect 1 file(s), or raise REVIEW_MAX_FILES_PER_REQUEST if this " +
      "repository needs it.";
    fetchMock.mockResolvedValue(refuses([{ msg: sent }]));

    expect(await run()).toBe(sent);
  });

  it("still says something when the server sends no detail at all", async () => {
    fetchMock.mockResolvedValue({ ok: false, status: 500, json: async () => ({}) });

    const message = await run();

    expect(message).toBe("Server error 500");
  });
});

it("keeps line findings scoped to source IDs and does not mistake them for completion", async () => {
  const id = FILES[0].source;
  const event = (meta: object) => `__STATUS__${JSON.stringify(meta)}__STATUS_END__\n`;
  const annotation = { step: "finding", id, origin: "static", rule_id: "tls", line: 2, title: "Verify TLS", message: "A reason", remediation: "A fix", severity: "high" };
  fetchMock.mockResolvedValue(new Response(
    event({ step: "analysis", id, content_sha256: "hash", finding_count: 1 }) +
    event(annotation) + event(annotation) + // duplicate events are idempotent
    event({ ...annotation, id: "other/source.py" }) +
    `__SECTION_START__${JSON.stringify({ id, file_name: "a.py" })}__SECTION_END__\n` +
    'Static fallback report\n' +
    event({ step: "complete", id, tier: "static", fallback_reason: "model call timed out", message: "Static fallback" })
  ));
  const { result } = renderHook(() => useMultiReview());
  await act(async () => { await result.current.reviewFiles(FILES); });
  expect(result.current.sections).toHaveLength(1);
  expect(result.current.sections[0].findings).toHaveLength(1);
  expect(result.current.sections[0].analysis?.contentHash).toBe("hash");
  expect(result.current.sections[0].fallbackReason).toBe("model call timed out");
  expect(result.current.llmReviewedCount).toBe(0);
  expect(result.current.agentSteps.some(s => s.step === "finding")).toBe(false);
});
