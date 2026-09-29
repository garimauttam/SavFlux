import { describe, expect, it, vi } from "vitest";
import { readIngestStream } from "./ingestStream";

function stream(...chunks: string[]) {
  return new Response(new ReadableStream({ start(controller) {
    for (const chunk of chunks) controller.enqueue(new TextEncoder().encode(chunk));
    controller.close();
  } }));
}

describe("ingestion completion", () => {
  it("buffers split SSE lines and recognizes a final success", async () => {
    const progress = vi.fn();
    await readIngestStream(stream('data: {"step":"cloning","message":"Cloning"}\n\nda',
      'ta: {"step":"com', 'plete","status":"success"}\n\n'), progress);
    expect(progress).toHaveBeenCalledWith({ step: "cloning", message: "Cloning" });
  });
  it("rejects incomplete streams rather than saving a branch as indexed", async () => {
    await expect(readIngestStream(stream('data: {"step":"cloning"}\n\n'), vi.fn())).rejects.toThrow(/did not report success/);
  });
  it("surfaces an explicit failure without allowing the selection to be saved", async () => {
    await expect(readIngestStream(stream('data: {"step":"complete","status":"error","message":"Clone failed"}\n\n'), vi.fn())).rejects.toThrow("Clone failed");
  });
});
