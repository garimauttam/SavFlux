/**
 * stream.ts — the client side of `app/services/stream_protocol.py`.
 *
 * Every streaming endpoint in SavFlux interleaves telemetry with prose in one
 * plain-text body:
 *
 *   __STATUS__{"step": "tool_done", "message": "…", "elapsed_ms": 12}__STATUS_END__
 *   __ERROR__…__ERROR_END__   __SOURCES__…__SOURCES_END__
 *   __SECTION_START__…__SECTION_END__   __DIAGNOSTIC__…__DIAGNOSTIC_END__
 *
 * Until now each consumer had its own copy of that parsing loop — five of them,
 * written five ways. They disagreed in ways that were invisible until a marker
 * arrived shaped slightly differently: `useChat` extracted the payload with
 * `/\{[^}]*\}$/`, which cannot cross a nested object, so a `coverage` marker (which
 * carries `provider_circuit`) parsed as its own substring; `AgentPanel` called
 * `JSON.parse` on the whole marker inside a bare `catch {}`, so a review-shaped
 * marker `text{json}` was dropped without a trace.
 *
 * This module is the single implementation of both halves: splitting a buffer into
 * ordered segments, and decoding one payload. It is deliberately dependency-free
 * and pure so it can be tested without React, and so a later move to real SSE or
 * WebSocket framing changes one file.
 *
 * CHUNK BOUNDARIES
 * ----------------
 * HTTP chunking can split a marker anywhere — including in the middle of
 * `__STATUS_END__`, which is the failure that used to leak raw markers into the
 * answer text. `drainMarkers` therefore holds back any tail that could still grow
 * into a marker (a prefix of an open tag, or an unfinished close tag) and never
 * emits it as prose. That is the whole reason this function exists rather than a
 * `split()`.
 */

export type MarkerKind = "status" | "error" | "sources" | "diagnostic" | "section" | "grounding";

export interface MarkerTag {
  kind: MarkerKind;
  open: string;
  close: string;
}

export const MARKER_TAGS: Record<MarkerKind, MarkerTag> = {
  status: { kind: "status", open: "__STATUS__", close: "__STATUS_END__" },
  error: { kind: "error", open: "__ERROR__", close: "__ERROR_END__" },
  sources: { kind: "sources", open: "__SOURCES__", close: "__SOURCES_END__" },
  /** What the finished answer cited, checked against the evidence it was given. */
  grounding: { kind: "grounding", open: "__GROUNDING__", close: "__GROUNDING_END__" },
  diagnostic: { kind: "diagnostic", open: "__DIAGNOSTIC__", close: "__DIAGNOSTIC_END__" },
  section: { kind: "section", open: "__SECTION_START__", close: "__SECTION_END__" },
};

/** What a review, agent or writer stream uses; chat adds sources and diagnostics. */
export const REVIEW_TAGS: MarkerTag[] = [
  MARKER_TAGS.status, MARKER_TAGS.error, MARKER_TAGS.section,
];
export const AGENT_TAGS: MarkerTag[] = [MARKER_TAGS.status, MARKER_TAGS.error];
export const CHAT_TAGS: MarkerTag[] = [
  MARKER_TAGS.status, MARKER_TAGS.sources, MARKER_TAGS.grounding,
  MARKER_TAGS.diagnostic, MARKER_TAGS.error,
];

export type Segment =
  | { kind: "text"; value: string }
  | { kind: MarkerKind; payload: string };

export interface ScanResult {
  /** Ordered pieces of this chunk: prose and complete markers, in stream order. */
  segments: Segment[];
  /** Trailing bytes that may still become a marker — feed them the next chunk. */
  rest: string;
}

/**
 * Split `buffer` into ordered segments, holding back an incomplete tail.
 *
 * Markers are consumed in stream order, so a consumer can route the prose before
 * a `__SECTION_START__` to the previous card and the prose after it to the next
 * one. Prose is only emitted in pieces when a marker actually interrupts it, which
 * keeps the common case (a chunk of review markdown) to one segment.
 */
export function drainMarkers(buffer: string, tags: MarkerTag[] = REVIEW_TAGS): ScanResult {
  const segments: Segment[] = [];
  let prose = "";
  let cursor = 0;
  // Set when an open tag arrived whose close has not: everything from here on is
  // an incomplete marker, so it is held verbatim rather than re-scanned as prose.
  let heldFrom = -1;

  for (;;) {
    const found = earliestTag(buffer, cursor, tags);
    if (!found) break;

    if (found.index > cursor) {
      prose += buffer.slice(cursor, found.index);
      cursor = found.index;
    }

    const bodyStart = cursor + found.tag.open.length;
    const closeIndex = buffer.indexOf(found.tag.close, bodyStart);
    if (closeIndex === -1) {
      heldFrom = cursor; // open tag seen, its close has not arrived yet
      break;
    }

    if (prose) {
      segments.push({ kind: "text", value: prose });
      prose = "";
    }
    segments.push({ kind: found.tag.kind, payload: buffer.slice(bodyStart, closeIndex) });
    cursor = consumeNewline(buffer, closeIndex + found.tag.close.length);
  }

  // Whatever is left is prose, except a tail that could still grow into a marker:
  // held back, because emitting it would put `__STATUS` in the answer text. The
  // cut is *only* at the risky suffix — holding whole trailing prose instead would
  // delay a chat answer until the stream ends, which is a visible one-chunk stutter
  // (or, with no further chunks, the whole answer arriving at once).
  let rest = heldFrom >= 0 ? buffer.slice(heldFrom) : buffer.slice(cursor);
  if (heldFrom < 0) {
    const hold = partialTagStart(rest, tags);
    if (hold > 0) {
      prose += rest.slice(0, hold);
      rest = rest.slice(hold);
    }
  }
  if (prose) segments.push({ kind: "text", value: prose });

  return { segments, rest };
}

function earliestTag(buffer: string, from: number, tags: MarkerTag[]) {
  let best: { index: number; tag: MarkerTag } | null = null;
  for (const tag of tags) {
    const index = buffer.indexOf(tag.open, from);
    if (index !== -1 && (best === null || index < best.index)) best = { index, tag };
  }
  return best;
}

function consumeNewline(buffer: string, index: number): number {
  return buffer[index] === "\n" ? index + 1 : index;
}

/**
 * Where a tail that could still become a marker begins.
 *
 * Returns `text.length` when the whole string is safe prose, so the caller holds
 * nothing back; a shorter index is the start of the longest suffix that is a proper
 * prefix of one of the tags (open *or* close, because a chunk can split either).
 */
function partialTagStart(text: string, tags: MarkerTag[]): number {
  if (!text) return 0;
  // Only the last `longest tag - 1` characters can be a partial marker, so the
  // scan is bounded by tag length rather than by chunk size.
  const longest = Math.max(...tags.map((tag) => Math.max(tag.open.length, tag.close.length)));
  const limit = Math.min(text.length, longest - 1);
  for (let length = limit; length > 0; length--) {
    const tail = text.slice(text.length - length);
    if (tags.some((tag) => tag.open.startsWith(tail) || tag.close.startsWith(tail))) {
      return text.length - length;
    }
  }
  return text.length;
}

/**
 * What to append when a stream ends: the held-back tail, minus anything that is
 * still telemetry rather than prose.
 *
 * A half-received `__STATUS__{"ste` is a telemetry line the server never
 * finished, not the last words of the answer — but ordinary prose that happens
 * to end with `__STAT` is prose, so only a *complete* open tag is cut.
 *
 * The loop matters more than the cut. A marker is defined by
 * `stream_protocol.py` as one JSON object on one line followed by a newline, so
 * an open tag with no close is a bad line, not a bad stream: the newline is
 * where the marker ends, and the answer after it is still the reader's. Cutting
 * everything from the first unterminated open tag — which is what a single
 * `rest.slice(0, index)` does — loses the whole reply to one malformed line and
 * shows an empty message with no error, which is the worst thing this module
 * could do. So the scan resumes past the newline and keeps going.
 */
export function flushTail(rest: string, tags: MarkerTag[] = REVIEW_TAGS): string {
  let out = "";
  let buf = rest;
  for (;;) {
    const found = earliestTag(buf, 0, tags);
    if (!found) return out + buf;

    const bodyStart = found.index + found.tag.open.length;
    const closeIndex = buf.indexOf(found.tag.close, bodyStart);

    out += buf.slice(0, found.index);
    if (closeIndex !== -1) {
      // Complete after all — it simply survived to the end of the buffer
      // without being scanned. Drop it, and keep looking.
      buf = buf.slice(consumeNewline(buf, closeIndex + found.tag.close.length));
      continue;
    }

    const newline = buf.indexOf("\n", bodyStart);
    // No newline: the stream really was cut mid-marker. There is nothing after
    // it that can be told apart from the payload, so the tail ends here.
    if (newline === -1) return out;
    buf = buf.slice(newline + 1);
  }
}

/**
 * The last resort for a stream that ended without a single line of prose.
 *
 * Every consumer builds its answer out of text segments, so a body that carried
 * only markers — or markers whose delimiters did not match the tags this build
 * knows — produces an empty assistant bubble. An empty bubble with no
 * explanation is the one failure a reader cannot act on: it looks like the model
 * had nothing to say, when in fact the bytes arrived and were dropped. So this
 * returns the raw tail with the protocol stripped out, and a line that says what
 * happened.
 *
 * Returns null when there is nothing worth showing, which keeps "the server
 * truncated its own telemetry and sent no answer" an honest blank rather than a
 * paragraph of JSON presented as prose.
 */
export function salvageUnreadableStream(
  rest: string,
  tags: MarkerTag[] = REVIEW_TAGS,
): string | null {
  if (!rest.trim()) return null;
  let text = rest;
  for (const tag of tags) {
    text = text.split(tag.open).join(" ").split(tag.close).join(" ");
  }
  // A delimiter the client could not match is still a delimiter, just a broken
  // one — `_STATUS_END_`, `__STATUS_END_`, `__SOURCES__`. The words are known,
  // because they come from the tags themselves, so match those with the
  // surrounding underscores loosened rather than guessing at the shape: a looser
  // pattern would eat the first word of the answer after the delimiter.
  const words = new Set<string>();
  for (const tag of tags) {
    for (const part of `${tag.open}${tag.close}`.split("_")) if (part) words.add(part);
  }
  if (words.size) {
    const alternation = [...words].join("|");
    text = text.replace(
      new RegExp(`_{1,3}(?:${alternation})(?:_(?:${alternation}))?_{0,3}`, "g"),
      " ",
    );
  }
  // What is left of a telemetry line is a JSON object, not an answer — and the
  // stream may have cut one in half, which is the whole reason we are here.
  text = text.replace(/\{"[^]*?\}/g, " ").replace(/\{"[^]*$/, " ");
  text = text.replace(/[ \t]{2,}/g, " ").trim();
  if (!/[A-Za-z]/.test(text)) return null;
  return [
    "**The stream ended before this answer could be read.** The server sent a",
    "response this build could not parse — the lines below are what arrived,",
    "verbatim:",
    "",
    `> ${text.slice(0, 600).replace(/\n+/g, "\n> ")}`,
    "",
    "Re-send the request. If it keeps happening, the server log for this",
    "request has the raw stream.",
  ].join("\n");
}

/** Any complete protocol marker in `text`? Used when a stream is read as a string. */export function containsMarker(text: string, tags: MarkerTag[] = REVIEW_TAGS): boolean {
  return tags.some((tag) => text.includes(tag.open) && text.includes(tag.close));
}

/** Prose only — markers removed. Mirrors `strip_protocol_markers` on the server. */
export function stripMarkers(text: string, tags: MarkerTag[] = REVIEW_TAGS): string {
  const { segments } = drainMarkers(text, tags);
  return segments.filter((segment) => segment.kind === "text").map((segment) => segment.value).join("");
}

/** A `__STATUS__` payload. Every field optional: producers emit different kinds. */
export interface StreamEvent {
  step?: string;
  message?: string;
  tool?: string;
  step_id?: string;
  plan_id?: string;
  ok?: boolean;
  elapsed_ms?: number;
  args?: Record<string, string>;
  preview?: string[];
  preview_more?: number;
  [key: string]: unknown;
}

/**
 * Decode one marker payload into an object.
 *
 * Mirrors `decode_status` in the Python protocol module, including the two legacy
 * shapes: `text{"…": …}` from a server built before `message` moved inside the
 * object, and a bare line of text. A marker that cannot be understood becomes a
 * message rather than a dropped step — a stream loop must not throw over a
 * telemetry line, and a silent drop is how a panel ends up "missing" the tool
 * calls it was sent.
 */
export function decodeStatus(raw: string): StreamEvent {
  const text = (raw ?? "").trim();
  if (!text) return {};

  const direct = parseObject(text);
  if (direct) {
    if (typeof direct.message !== "string") direct.message = "";
    return direct;
  }

  // Legacy `text{json}`: the last `{` that starts a valid object wins, so braces
  // inside the message ("the dict {'a': 1} is suspicious") cannot fool it.
  for (let index = text.indexOf("{"); index !== -1; index = text.indexOf("{", index + 1)) {
    const parsed = parseObject(text.slice(index));
    if (parsed) {
      if (typeof parsed.message !== "string") parsed.message = text.slice(0, index).trim();
      return parsed;
    }
  }

  return { message: text };
}

function parseObject(text: string): StreamEvent | null {
  if (!text.startsWith("{")) return null;
  try {
    const value: unknown = JSON.parse(text);
    return value && typeof value === "object" && !Array.isArray(value) ? (value as StreamEvent) : null;
  } catch {
    return null;
  }
}

/** `912 ms`, `1.4 s`, `2 m 05 s` — one format for every duration the UI shows. */
export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || Number.isNaN(ms)) return "";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  const seconds = ms / 1000;
  if (seconds < 60) return `${seconds.toFixed(seconds < 10 ? 2 : 1)} s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes} m ${(Math.round(seconds) - minutes * 60).toString().padStart(2, "0")} s`;
}
