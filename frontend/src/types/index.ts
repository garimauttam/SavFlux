// types/index.ts — shared TypeScript interfaces across the whole frontend
// Defining these once means if the API changes, you fix it in one place.

export interface Message {
  id: string;
  role: "user" | "assistant";
  content: string;
  sources?: SourceFile[];  // citations from the RAG retrieval
  generationSteps?: string[]; // safe retrieval/model activity trace
  isStreaming?: boolean;   // true while the answer is being typed out
}

/** Trust level for a citation, derived from the cross-encoder relevance score.
 *  "unrated" means reranking was skipped or unavailable — the chunk was never
 *  judged, which is NOT the same as being judged and found weak. */
export type TrustLevel = "high" | "medium" | "low" | "unrated";

export interface SourceFile {
  file_name: string;
  source: string;    // stable source id, e.g. "https://github.com/o/r::src/auth.py"
  language: string;  // "py", "js", etc.
  // Trust-ledger enrichment (optional — older __SOURCES__ payloads omit these)
  trust_level?: TrustLevel | string;
  /** Cross-encoder score. null when the chunk was never reranked. */
  trust_score?: number | string | null;
  /** 1-indexed inclusive span of the cited evidence in the original file. */
  start_line?: number;
  end_line?: number;
  /** Exact cited regions as "1-30,88-92" when the evidence is discontinuous
   *  (e.g. a module chunk covering imports plus scattered constants). */
  line_ranges?: string;
  symbol_name?: string;
  chunk_index?: number;
  /** How many retrieved chunks from this file backed the answer. */
  chunk_count?: number;
  score?: number;
}

export interface IngestionProgress {
  step: "cloning" | "scanning" | "splitting" | "embedding" | "done" | "complete" | "error";
  message: string;
  files_indexed?: number;
  chunks_created?: number;
}

export interface IndexedFile {
  file_name: string;
  language: string;
  repo_url: string;
  source: string;
}

export interface IndexedRepo {
  repo_url: string;
  chunk_count: number;
}

export interface ChatRequest {
  question: string;
  chat_history: { role: string; content: string }[];
  active_repo_url?: string | null;
}
