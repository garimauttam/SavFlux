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

export interface SourceFile {
  file_name: string;
  source: string;    // full path on disk
  language: string;  // "py", "js", etc.
  // Trust-ledger enrichment (optional — older __SOURCES__ payloads omit these)
  trust_level?: "high" | "medium" | "low" | string;
  trust_score?: number | string;
  start_line?: number;
  end_line?: number;
  symbol_name?: string;
  chunk_index?: number;
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
