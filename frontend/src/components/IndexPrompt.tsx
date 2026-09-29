/**
 * IndexPrompt.tsx — the "nothing indexed yet" state, with the way out on it.
 *
 * WHY THIS EXISTS
 * ---------------
 * Four pages can be the first page someone sees, and each used to handle "no
 * repository" by naming somewhere else:
 *
 *     Agent       "Or paste a public URL, or upload a folder"   (no control)
 *     Review      "No files indexed yet. Add a repo in the sidebar first."
 *     Files       "No files indexed yet — ingest a repo to see tree."
 *     Graph       "Index a GitHub repo to see its dependency graph."
 *
 * Not one of them had the control. The sentence is a promise, and a promise
 * with no button is a dead end — worst on Review, which pointed at a sidebar
 * that does not exist (the repository switcher is in the top bar).
 *
 * The rule this component enforces: a page may not tell the reader to go
 * somewhere else to do the thing it needs. If the page says it needs an index,
 * the page indexes. One component, so the four copies cannot drift.
 *
 * It reuses `usePublicIngest`, the same hook the Agent page uses, so the copy
 * and the capability stay in one place rather than being restated per panel.
 */

import type { OnIndexed } from "../lib/repositorySelection";

import { useRef } from "react";
import { Loader2, Upload } from "lucide-react";
import { RepositoryIndexForm } from "./RepositoryIndexForm";
import { usePublicIngest } from "../hooks/usePublicIngest";

export interface IndexPromptProps {
  /** What this page will show once there is something to read. */
  title: string;
  /** One sentence on what indexing gives you here. */
  body: string;
  /** Re-read the index once it is built. */
  onIndexed: OnIndexed;
  /** `sm` for a panel inset in a page, `md` for a full empty page. */
  size?: "sm" | "md";
  className?: string;
}

export function IndexPrompt({
  title,
  body,
  onIndexed,
  size = "sm",
  className = "",
}: IndexPromptProps) {
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const { ingesting, progress, error, ingestUrl, upload } = usePublicIngest(onIndexed);

  return (
    <div className={`flex flex-col items-center justify-center text-center ${className}`}>
      <div className={size === "md" ? "max-w-md w-full space-y-4" : "max-w-sm w-full space-y-3"}>
        <div>
          <h3 className="sf-text text-[15px] font-semibold tracking-tight">{title}</h3>
          <p className="sf-mute mt-1 text-[12.5px] leading-relaxed">{body}</p>
        </div>

        <RepositoryIndexForm busy={!!ingesting} onIndex={ingestUrl} />

        <label className="sf-btn sf-btn-ghost w-full cursor-pointer justify-center">
          <Upload className="h-3.5 w-3.5" />
          {ingesting ? "Indexing…" : "Upload files from this computer"}
          <input
            ref={fileInputRef}
            type="file"
            multiple
            className="sr-only"
            onChange={(e) => void upload(e.target.files)}
          />
        </label>

        {progress && (
          <p className="sf-mute flex items-center justify-center gap-1.5 text-[11.5px]">
            <Loader2 className="h-3 w-3 animate-spin" />
            {progress.message}
          </p>
        )}

        {error && (
          <p
            role="alert"
            className="rounded-lg px-3 py-2 text-left text-[11.5px]"
            style={{ background: "rgba(248,113,113,0.1)", color: "#fca5a5" }}
          >
            {error}
          </p>
        )}
      </div>
    </div>
  );
}
