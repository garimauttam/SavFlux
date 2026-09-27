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
  containsMarker, decodeStatus, drainMarkers, flushTail, formatDuration, stripMarkers,
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
      `${MARKER_TAGS.diagnostic.open}P01 retrieval drift${MARKER_TAGS.diagnostic.close}\n`;
    const kinds = drainMarkers(stream, CHAT_TAGS).segments.map((segment) => segment.kind);
    expect(kinds).toEqual(["text", "sources", "text", "diagnostic"]);
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
