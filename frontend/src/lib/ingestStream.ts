export interface IngestEvent {
  step?: string;
  status?: string;
  message?: string;
  files_indexed?: number;
}

/** SSE lines may cross fetch chunks. Only an explicit complete/success is success. */
export async function readIngestStream(response: Response, onProgress: (event: IngestEvent) => void): Promise<void> {
  const reader = response.body?.getReader();
  if (!reader) throw new Error("Indexing returned no progress stream. Please retry.");
  const decoder = new TextDecoder();
  let buffer = "";
  let result: IngestEvent | null = null;
  const consume = (line: string) => {
    const match = /^data:\s*(.*)$/.exec(line.trim());
    if (!match) return;
    let event: IngestEvent;
    try { event = JSON.parse(match[1]); }
    catch { return; }
    if (event.step === "complete") result = event;
    else onProgress(event);
  };
  try {
    for (;;) {
      const { done, value } = await reader.read();
      buffer += done ? decoder.decode() : decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() ?? "";
      lines.forEach(consume);
      if (done) { if (buffer) consume(buffer); break; }
    }
  } finally { reader.releaseLock(); }
  // A helper keeps TypeScript's control-flow analysis aware of callback writes.
  const completion = result as IngestEvent | null;
  if (completion?.status !== "success") {
    throw new Error(completion?.message || "Indexing did not report success. Please retry before using this branch.");
  }
}
