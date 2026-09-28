/**
 * useChat.test.ts — the chat consumer, driven through the reader loop.
 *
 * WHY THIS FILE EXISTS
 * --------------------
 * The stream parser is unit-tested and correct. The bug it could not catch was
 * one layer up: `useChat` builds its answer only from *text* segments, so a
 * response the parser could not segment — a body whose delimiters did not match
 * the tags this build knows — produced an assistant message with empty content
 * and no error. It rendered as a blank bubble, which reads as "the model had
 * nothing to say" when the bytes in fact arrived and were dropped.
 *
 * The parser cannot be tested for this, because the parser was right: given a
 * marker with no close tag, holding it back is the only safe reading *while the
 * stream is still open*. Deciding what to show when the stream has ended is the
 * consumer's job, so it is tested here, through the hook, on the real reader
 * loop.
 *
 * The mangled body below is the one that actually happened: a producer wrote
 * `_STATUS_END_` where the protocol says `__STATUS_END__`, and the whole reply
 * vanished.
 */
import { afterEach, describe, expect, it, vi, type Mock } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import { apiFetch } from "../api";
import type { Message } from "../types";
import { useChat } from "./useChat";

vi.mock("../api", () => ({ apiFetch: vi.fn() }));
const fetchMock = apiFetch as unknown as Mock;

afterEach(() => {
  fetchMock.mockReset();
  localStorage.clear();
});

/** A body delivered as `chunks`, so the reader loop is exercised for real. */
function respond(chunks: string[]) {
  let index = 0;
  return {
    ok: true,
    body: {
      getReader: () => ({
        read: async () =>
          index < chunks.length
            ? { done: false, value: new TextEncoder().encode(chunks[index++]) }
            : { done: true, value: undefined },
      }),
    },
  };
}

const last = (messages: Message[]) => messages[messages.length - 1];

describe("useChat — a well-formed stream", () => {
  it("renders the answer, the steps and the sources", async () => {
    fetchMock.mockResolvedValue(
      respond([
        '__STATUS__{"step":"retrieving","message":"Searching 2,830 windows…"}\n__STATUS_END__\n',
        "Signature verification happens in `token_backend.verify`.\n",
        '__SOURCES__[{"file_name":"core.py","source":"https://github.com/o/r::src/click/core.py",' +
          '"language":"py","trust_level":"high","start_line":42,"end_line":58}]\n__SOURCES_END__',
      ]),
    );

    const { result } = renderHook(() => useChat(null));
    await act(async () => { await result.current.sendMessage("where do we verify?"); });

    const answer = last(result.current.messages);
    expect(answer.role).toBe("assistant");
    expect(answer.content).toContain("token_backend.verify");
    expect(answer.sources).toHaveLength(1);
    expect(answer.sources![0].file_name).toBe("core.py");
    expect(answer.generationSteps).toContain("Searching 2,830 windows…");
    expect(answer.isStreaming).toBe(false);
  });
});

describe("useChat — the grounding check", () => {
  it("carries the report so the reader can be told", async () => {
    const report = {
      citations_claimed: 2,
      unverified: [{
        reference: "ghost.py:76-83",
        file_name: "ghost.py",
        line_start: 76,
        line_end: 83,
        reason: "not_in_context",
        detail: "`ghost.py` was cited but is not among the 1 file(s) this answer was given",
      }],
      unverified_count: 1,
      insufficient_evidence: false,
      delivered_anyway: true,
      matched_phrase: "",
      is_clean: false,
    };
    fetchMock.mockResolvedValue(
      respond([
        "The check is in `token_backend.verify`.\n",
        '__SOURCES__[{"file_name":"core.py","source":"https://github.com/o/r::src/click/core.py",' +
          '"language":"py","trust_level":"high","start_line":42,"end_line":58}]\\n__SOURCES_END__',
        `__GROUNDING__${JSON.stringify(report)}__GROUNDING_END__\n`,
      ]),
    );

    const { result } = renderHook(() => useChat(null));
    await act(async () => { await result.current.sendMessage("where do we verify?"); });

    const answer = last(result.current.messages);
    expect(answer.grounding?.is_clean).toBe(false);
    expect(answer.grounding?.unverified[0].reference).toBe("ghost.py:76-83");
    // The answer itself must survive the check — it reports, it does not censor.
    expect(answer.content).toContain("token_backend.verify");
  });

  it("a clean report is kept, so the UI can tell checked from unchecked", async () => {
    fetchMock.mockResolvedValue(
      respond([
        "The check is in `core.py:42-58`.\n",
        `__GROUNDING__${JSON.stringify({
          citations_claimed: 1, unverified: [], unverified_count: 0,
          insufficient_evidence: false, delivered_anyway: true,
          matched_phrase: "", is_clean: true,
        })}__GROUNDING_END__\n`,
      ]),
    );

    const { result } = renderHook(() => useChat(null));
    await act(async () => { await result.current.sendMessage("where do we verify?"); });

    expect(last(result.current.messages).grounding?.is_clean).toBe(true);
  });

  it("a malformed report is dropped rather than guessed at", async () => {
    fetchMock.mockResolvedValue(
      respond(["An answer.\n", "__GROUNDING__{not json__GROUNDING_END__\n"]),
    );

    const { result } = renderHook(() => useChat(null));
    await act(async () => { await result.current.sendMessage("q"); });

    const answer = last(result.current.messages);
    expect(answer.grounding).toBeUndefined();
    expect(answer.content).toContain("An answer.");
  });
});

describe("useChat — a stream the parser could not read", () => {
  it("never renders an empty answer, and says what arrived instead", async () => {
    // The real body: `_STATUS_END_` is one underscore short on each side, so
    // `__STATUS__` opens a marker that never closes. `drainMarkers` holds the
    // whole response; `flushTail` used to cut at index 0 and the bubble was
    // empty.
    fetchMock.mockResolvedValue(
      respond([
        '__STATUS__{"step":"retrieving","message":"Searching 2,830 windows…"}_STATUS_END_',
        "Signature verification happens in `token_backend.verify`.",
      ]),
    );

    const { result } = renderHook(() => useChat(null));
    await act(async () => { await result.current.sendMessage("where do we verify?"); });

    const answer = last(result.current.messages);
    // The reader must be able to tell what went wrong and what the server said.
    expect(answer.content).not.toBe("");
    expect(answer.content).toContain("could be read");
    expect(answer.content).toContain("token_backend.verify");
    // And the protocol must not be reprinted as if it were the answer.
    expect(answer.content).not.toContain("__STATUS__");
    expect(answer.content).not.toContain('"step":"retrieving"');
    expect(answer.isStreaming).toBe(false);
  });

  it("does not invent an answer out of telemetry alone", async () => {
    // Nothing but a marker, truncated mid-payload. There is no answer to
    // recover, and a paragraph of JSON presented as prose would be worse than
    // the blank — so this stays blank, with no error, because the stream did
    // not fail; it simply had nothing to say.
    fetchMock.mockResolvedValue(respond(['__STATUS__{"step":"wri']));

    const { result } = renderHook(() => useChat(null));
    await act(async () => { await result.current.sendMessage("hello"); });

    const answer = last(result.current.messages);
    expect(answer.content).toBe("");
    expect(answer.isStreaming).toBe(false);
    expect(result.current.error).toBeNull();
  });
});

describe("useChat — persistence", () => {
  it("keeps history per repository and never restores a spinner", async () => {
    fetchMock.mockResolvedValue(respond(["done\n"]));

    const first = renderHook(() => useChat("https://github.com/o/a"));
    await act(async () => { await first.result.current.sendMessage("q"); });
    expect(localStorage.getItem("savflux:messages:https://github.com/o/a")).toContain("done");

    // A different repository is a different thread, not a shared one.
    const other = renderHook(() => useChat("https://github.com/o/b"));
    expect(other.result.current.messages).toEqual([]);
  });

  it("drops a still-streaming message from storage, so a reload shows no spinner", () => {
    localStorage.setItem(
      "savflux:messages:https://github.com/o/a",
      JSON.stringify([{ id: "1", role: "assistant", content: "x", isStreaming: true }]),
    );
    const { result } = renderHook(() => useChat("https://github.com/o/a"));
    expect(result.current.messages[0].isStreaming).toBe(false);
  });
});

describe("useChat — errors", () => {
  it("surfaces a non-2xx response instead of leaving a blank bubble", async () => {
    fetchMock.mockResolvedValue({ ok: false, status: 502 });
    const { result } = renderHook(() => useChat(null));
    await act(async () => { await result.current.sendMessage("hello"); });

    await waitFor(() => expect(result.current.error).toBeTruthy());
    expect(result.current.error).toContain("502");
    expect(last(result.current.messages).content).not.toBe("");
  });
});
