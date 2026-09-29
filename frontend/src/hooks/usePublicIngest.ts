/**
 * usePublicIngest.ts — indexing code the product has no credentials for.
 *
 * WHY IT IS A HOOK AND NOT A PROP
 * -------------------------------
 * Ingesting a public URL and uploading local files are two operations, each
 * about ten lines of fetch plus a progress channel, and they were needed in two
 * places before they were extracted: the Repositories panel, and the agent's
 * empty state. The agent's empty state had *promised* them —
 *
 *     "Or paste a public URL, or upload a folder — no account needed."
 *
 * — with no control behind the sentence, because the panel that implemented it
 * lived one destination away. A reader who believed the promise and clicked
 * nothing found a dead end on the first screen of the product. The copy was not
 * wrong about the capability; it was wrong about where the capability was.
 *
 * So the capability and the copy live together, and the promise is kept by
 * construction rather than by remembering to update two places.
 *
 * WHY THIS NEEDS NO TOKEN
 * -----------------------
 * A public repository is cloned with no credential at all: `get_token()`
 * returns "" when GitHub is not connected and `auth_clone_kwargs()` returns
 * `{}`, so the unauthenticated API is used. This is the path that has to work
 * first, because it is the only one that works on a machine where the reader
 * has not connected anything.
 */

import { readIngestStream } from "../lib/ingestStream";
import type { OnIndexed } from "../lib/repositorySelection";
import { useCallback, useRef, useState } from "react";
import { apiFetch } from "../api";
import { apiError } from "./useIntegrations";

export interface IngestProgress {
  /** What is being indexed — a URL, or a file name. */
  target: string;
  message: string;
}

export interface PublicIngest {
  /** Non-null while a job is running; names what it is. */
  ingesting: string | null;
  progress: IngestProgress | null;
  error: string | null;
  /** Index a public repository by URL. Resolves true when the index was built. */
  ingestUrl: (url: string, branch?: string) => Promise<boolean>;
  /** Upload local files. Resolves true when the index was built. */
  upload: (files: FileList | null) => Promise<boolean>;
  clearError: () => void;
}

export function usePublicIngest(onIndexed: OnIndexed): PublicIngest {
  const [ingesting, setIngesting] = useState<string | null>(null);
  const [progress, setProgress] = useState<IngestProgress | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Resetting a file input's value is the only way to let the same file be
  // chosen twice in a row; without it, re-uploading a corrected file silently
  // does nothing, which reads as a broken upload button.
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  const ingestUrl = useCallback(
    async (raw: string, branch = "") => {
      const url = raw.trim();
      if (!url) return false;
      setIngesting(url);
      setError(null);
      setProgress({ target: url, message: "Cloning…" });
      try {
        const res = await apiFetch("/api/v1/ingest/github", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ repo_url: url, branch: branch.trim() }),
        });
        if (!res.ok) throw new Error(await apiError(res, "Could not start the index"));

        await readIngestStream(res, (event) => {
          if (!event.message) return;
          setProgress({
            target: url,
            message: event.files_indexed ? `${event.message} · ${event.files_indexed} files` : event.message,
          });
        });
        onIndexed({ repoUrl: url, branch: branch.trim() });
        return true;
      } catch (e) {
        // `friendly_ingest_error` has already turned a network failure into
        // something a reader can act on by the time it reaches here.
        setError(e instanceof Error ? e.message : "Could not start the index");
        return false;
      } finally {
        setIngesting(null);
        setProgress(null);
      }
    },
    [onIndexed],
  );

  const upload = useCallback(
    async (files: FileList | null) => {
      if (!files?.length) return false;
      setError(null);
      const target = files.length === 1 ? files[0].name : `${files.length} files`;
      setIngesting(target);
      setProgress({ target, message: "Uploading…" });
      try {
        const body = new FormData();
        Array.from(files).forEach((file) => body.append("files", file));
        const res = await apiFetch("/api/v1/ingest/files", { method: "POST", body });
        if (!res.ok) throw new Error(await apiError(res, "Upload failed"));
        onIndexed();
        return true;
      } catch (e) {
        setError(e instanceof Error ? e.message : "Upload failed");
        return false;
      } finally {
        setIngesting(null);
        setProgress(null);
        if (fileInputRef.current) fileInputRef.current.value = "";
      }
    },
    [onIndexed],
  );

  const clearError = useCallback(() => setError(null), []);

  return { ingesting, progress, error, ingestUrl, upload, clearError };
}
