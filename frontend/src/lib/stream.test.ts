/**
 * stream.test.ts — the client half of the wire protocol.
 *
 * Two contracts, and the tests are written to fail if either is "simplified":
 *
 *   1. a marker split across chunks is never emitted as prose, in any of its
 *      positions (start, middle, end);
 *   2. every stream's markers decode to the same object shape, whether the payload
 *      arrived in the canonical `{"message": …}` form or the legacy `text{json}`
 *      form — because a browser bundle and a server process are not updated at
 *      the same instant.
 */
import { describe, expect, it } from "vitest";
import {
  AGENT_TAGS, CHAT_TAGS, MARKER_TAGS, REVIEW_TAGS,
  containsMarker, decodeStatus, drainMarkers, flushTail, formatDuration, salvageUnreadableStream,
  stripMarkers,
} from "./stream";

const status = (payload: string) => `${MARKER_TAGS.status.open}${payload}${MARKER_TAGS.status.close}\n`;

describe("drainMarkers", () => {
  it("passes plain prose through untouched", () => {
    const scan = drainMarkers("## Bugs\nNone found.\n");
    expect(scan.segments).toEqual([{ kind: "text", value: "## Bugs\nNone found.\n" }]);
    expect(scan.rest).toBe("");
  });

  it("splits a marker out of the prose around it", () => {
    const scan = drainMarkers(`before${status('{"step":"x","message":"m"}')}after`);
    expect(scan.segments).toEqual([
      { kind: "text", value: "before" },
      { kind: "status", payload: '{"step":"x","message":"m"}' },
      { kind: "text", value: "after" },
    ]);
  });

  it("consumes the newline after a marker but not the answer's own", () => {
    const scan = drainMarkers(`${status('{"step":"x"}')}\n## Heading`);
    expect(scan.segments[1]).toEqual({ kind: "text", value: "\n## Heading" });
  });

  it("holds back a tail that could still become a marker", () => {
    // The failure this exists for: "__STA" must not reach the answer text, because
    // the next chunk completes "__STATUS_END__" and the reader would see a tag.
    expect(drainMarkers("the answer __STA").rest).toBe("__STA");
    expect(drainMarkers("the answer __STA").segments).toEqual([
      { kind: "text", value: "the answer " },
    ]);
  });

  it("holds a complete open tag until its close arrives", () => {
    const scan = drainMarkers(`x${MARKER_TAGS.status.open}{"step":"a"`);
    expect(scan.segments).toEqual([{ kind: "text", value: "x" }]);
    expect(scan.rest).toBe(`${MARKER_TAGS.status.open}{"step":"a"`);

    // The held bytes are still a marker once the close lands, even if the payload
    // inside them is garbage — garbage decodes to a message, never to prose.
    const done = drainMarkers(`x${MARKER_TAGS.status.open}{"step":"a"}${MARKER_TAGS.status.close}`);
    expect(done.segments).toEqual([
      { kind: "text", value: "x" },
      { kind: "status", payload: '{"step":"a"}' },
    ]);
  });

  it("reassembles a marker split across three chunks", () => {
    const full = status('{"step":"tool_done","message":"done","elapsed_ms":9}');
    const pieces = [full.slice(0, 8), full.slice(8, 30), full.slice(30)];
    const seen: string[] = [];
    let buffer = "";
    let prose = "";

    for (const piece of pieces) {
      buffer += piece;
      const scan = drainMarkers(buffer, AGENT_TAGS);
      buffer = scan.rest;
      for (const segment of scan.segments) {
        if (segment.kind === "text") prose += segment.value;
        else seen.push(segment.payload);
      }
    }

    expect(seen).toEqual(['{"step":"tool_done","message":"done","elapsed_ms":9}']);
    expect(prose).toBe("");
    expect(drainMarkers(buffer, AGENT_TAGS).rest).toBe("");
  });

  it("never turns a truncated marker into prose, even at flush", () => {
    const tail = `${MARKER_TAGS.status.open}{"step":"writ`;
    expect(flushTail(tail, AGENT_TAGS)).toBe("");
    expect(flushTail("the end of the sentence", AGENT_TAGS)).toBe("the end of the sentence");
    // Prose that merely *looks* like a partial tag is prose once the stream ends.
    expect(flushTail("error code __STAT", AGENT_TAGS)).toBe("error code __STAT");
  });

  it("recovers the answer after a marker that never closed", () => {
    // A close tag the client cannot match — a producer bug, a proxy that
    // rewrote the body, a version skew. The reply used to disappear entirely:
    // the scan held from the first open tag to the end of the response and
    // flush cut at index 0, so the reader saw an empty message and no error.
    // `stream_protocol.py` puts a marker on a line of its own, so the newline
    // is where the unreadable marker ends and the answer resumes.
    const mangled = [
      `${MARKER_TAGS.status.open}{"step":"retrieving"}_STATUS_END_\n`,
      "Signature verification happens in `token_backend.verify`.\n",
    ].join("");
    expect(flushTail(mangled, CHAT_TAGS)).toBe("Signature verification happens in `token_backend.verify`.\n");
  });

  it("resumes after a bad line and still parses the markers that follow", () => {
    const body = [
      `${MARKER_TAGS.status.open}{"step":"broken"}_STATUS_END_\n`,
      status('{"step":"reranking"}'),
      "## Answer\n",
      status('{"step":"done"}'),
      "last line",
    ].join("");
    // The good markers are recovered by drainMarkers; the tail is prose only.
    const scan = drainMarkers(body, CHAT_TAGS);
    const prose = scan.segments
      .filter((segment): segment is { kind: "text"; value: string } => segment.kind === "text")
      .map((segment) => segment.value)
      .join("");
    expect(prose + flushTail(scan.rest, CHAT_TAGS)).toBe("## Answer\nlast line");
  });

  it("keeps prose that precedes a truncated marker", () => {
    const body = `The answer so far.\n${MARKER_TAGS.status.open}{"step":"wri`;
    expect(flushTail(body, AGENT_TAGS)).toBe("The answer so far.\n");
  });

  it("keeps section order so prose lands on the card it belongs to", () => {
    const stream = [
      "prose-for-a",
      `${MARKER_TAGS.section.open}{"id":"b"}${MARKER_TAGS.section.close}\n`,
      "prose-for-b",
    ].join("");
    expect(drainMarkers(stream, REVIEW_TAGS).segments.map((segment) => segment.kind)).toEqual([
      "text", "section", "text",
    ]);
  });

  it("handles chat's three extra marker kinds in one pass", () => {
    const stream =
      "answer " +
      `${MARKER_TAGS.sources.open}[{"file":"a.py"}]${MARKER_TAGS.sources.close}\n` +
      "more " +
      `${MARKER_TAGS.diagnostic.open}P01 retrieval drift${MARKER_TAGS.diagnostic.close}\n` +
      "and " +
      `${MARKER_TAGS.grounding.open}{"is_clean":false}${MARKER_TAGS.grounding.close}\n`;
    const kinds = drainMarkers(stream, CHAT_TAGS).segments.map((segment) => segment.kind);
    expect(kinds).toEqual(["text", "sources", "text", "diagnostic", "text", "grounding"]);
  });

  it("the grounding marker is chat-only, so a review stream cannot emit one", () => {
    // A review or agent stream is not passed the sources it would be checked
    // against; honouring the marker there would render a check with no basis.
    const stream = `a${MARKER_TAGS.grounding.open}{"is_clean":false}${MARKER_TAGS.grounding.close}`;
    const scan = drainMarkers(stream, REVIEW_TAGS);

    // Not parsed as a marker — it stays prose, and the text is not lost: the
    // scanner holds a trailing `__` back because it could still become one.
    expect(scan.segments.every((segment) => segment.kind === "text")).toBe(true);
    const prose = scan.segments.map((segment) => (segment as { value: string }).value).join("");
    expect(prose + flushTail(scan.rest, REVIEW_TAGS)).toBe(stream);
  });

  it("an unterminated grounding marker is held back rather than shown as prose", () => {
    const stream = `answer\n${MARKER_TAGS.grounding.open}{"is_c`;
    const scan = drainMarkers(stream, CHAT_TAGS);
    expect(scan.rest).toBe(`${MARKER_TAGS.grounding.open}{"is_c`);
    expect(flushTail(scan.rest, CHAT_TAGS)).toBe("");
  });

  it("reports markers and strips them for a string-only reader", () => {
    const stream = `intro ${status('{"step":"x","message":"m"}')} outro`;
    expect(containsMarker(stream)).toBe(true);
    expect(stripMarkers(stream)).toBe("intro  outro");
    expect(containsMarker("no markers here")).toBe(false);
  });
});

describe("decodeStatus", () => {
  it("reads the canonical form", () => {
    expect(decodeStatus('{"step":"tool_done","message":"ok","elapsed_ms":12}')).toEqual({
      step: "tool_done", message: "ok", elapsed_ms: 12,
    });
  });

  it("reads the legacy text-then-json form and keeps the text as the message", () => {
    const decoded = decodeStatus('Scanning `a.py`...{"step":"starting","mode":"fast"}');
    expect(decoded).toEqual({ step: "starting", mode: "fast", message: "Scanning `a.py`..." });
  });

  it("reads a payload with nested objects, which the old regex could not", () => {
    // `useChat` used /(\{[^}]*\})$/ for this split; the review stream's `coverage`
    // token carries `provider_circuit`, a nested object the regex cannot cross.
    const legacy = 'Coverage: 1/2 LLM reviews...{"step":"coverage","total":2,"provider_circuit":{"open":false,"failures":0}}';
    const decoded = decodeStatus(legacy);
    expect(decoded.step).toBe("coverage");
    expect(decoded.total).toBe(2);
    expect(decoded.provider_circuit).toEqual({ open: false, failures: 0 });
    expect(decoded.message).toBe("Coverage: 1/2 LLM reviews...");
  });

  it("prefers the object's own message over a legacy prefix", () => {
    expect(decodeStatus('stale {"step":"x","message":"real"}').message).toBe("real");
  });

  it("treats a bare line as a message rather than an error", () => {
    expect(decodeStatus("Generating answer...")).toEqual({ message: "Generating answer..." });
  });

  it("never throws on garbage", () => {
    expect(decodeStatus("")).toEqual({});
    expect(decodeStatus("{oops")).toEqual({ message: "{oops" });
    expect(decodeStatus("[1,2]")).toEqual({ message: "[1,2]" });
    expect(decodeStatus("null")).toEqual({ message: "null" });
  });

  it("decodes the canonical and legacy form of the same step identically", () => {
    /**
     * The compatibility claim in one assertion: whatever a server's version, the
     * client's step list says the same thing. `message` is where the two differ on
     * the wire, so it must agree here or a panel silently loses its labels.
     */
    const canonical = decodeStatus('{"step":"file","message":"Reviewing `a.py`","file":"a.py"}');
    const legacy = decodeStatus('Reviewing `a.py`{"step":"file","file":"a.py"}');
    expect(canonical).toEqual(legacy);
  });
});

describe("formatDuration", () => {
  it("scales the unit with the number", () => {
    expect(formatDuration(0)).toBe("0 ms");
    expect(formatDuration(812)).toBe("812 ms");
    expect(formatDuration(1400)).toBe("1.40 s");
    expect(formatDuration(12400)).toBe("12.4 s");
    expect(formatDuration(65_000)).toBe("1 m 05 s");
    expect(formatDuration(null)).toBe("");
  });
});

describe("salvageUnreadableStream", () => {
  it("explains a body whose delimiters never matched", () => {
    // The body that produced a blank answer in the first screenshot run: the
    // producer wrote `_STATUS_END_` and the client knew only `__STATUS_END__`,
    // so the whole response was held back and then cut.
    const body = [
      '__STATUS__{"step":"retrieving","message":"Searching 2,830 windows across 147 files…"}_STATUS_END_',
      "Signature verification happens in `token_backend.verify`.",
    ].join("");

    const salvaged = salvageUnreadableStream(body, CHAT_TAGS);
    expect(salvaged).toBeTruthy();
    expect(salvaged).toContain("Signature verification happens in `token_backend.verify`.");
    // The protocol must not be reprinted as if it were the answer.
    expect(salvaged).not.toContain("__STATUS__");
    expect(salvaged).not.toContain("_STATUS_END_");
    expect(salvaged).not.toContain('"step":"retrieving"');
  });

  it("has nothing to say about an empty tail", () => {
    expect(salvageUnreadableStream("", CHAT_TAGS)).toBeNull();
    expect(salvageUnreadableStream("   \n ", CHAT_TAGS)).toBeNull();
    // Telemetry only, no prose: showing a paragraph of JSON as an answer is
    // worse than showing nothing.
    expect(salvageUnreadableStream('__STATUS__{"step":"wri', CHAT_TAGS)).toBeNull();
  });

  it("strips well-formed markers too, since the caller only reaches it with no prose", () => {
    const body = `${status('{"step":"done"}')}\nAll three gates passed.\n`;
    expect(salvageUnreadableStream(body, CHAT_TAGS)).toContain("All three gates passed.");
  });
});
