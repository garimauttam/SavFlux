/**
 * StopButton.tsx — the control that ends a run on both sides of the wire.
 *
 * It looks like the agent console's Stop on purpose, and it does the same two things:
 * abort the fetch, and let the caller draw its own stopped state (nothing further
 * arrives to say where the run ended, because the abort closed the reader).
 *
 * The copy matters more than it looks. Every review, chat and writer stream already
 * refuses to spend a model call once the reader is gone; a button labelled "Cancel"
 * next to a partial review would suggest the work was thrown away, when the sections
 * that finished are still on the page. "Stop" is the truth: it ends what has not run.
 */

import { Square } from "lucide-react";

export interface StopButtonProps {
  onClick: () => void;
  /** Overrides the label; the accessible name stays "Stop" either way. */
  label?: string;
  className?: string;
}

export function StopButton({ onClick, label = "Stop", className = "" }: StopButtonProps) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label="Stop"
      title="Stop this run. The server stops the model too — nothing else is generated."
      className={`flex items-center justify-center gap-1.5 rounded-xl bg-red-500/15 px-3 py-2 text-xs font-medium text-red-300 ring-1 ring-inset ring-red-500/30 transition-colors hover:bg-red-500/25 hover:text-red-200 focus:outline-none focus-visible:ring-2 focus-visible:ring-red-500/60 ${className}`}
    >
      <Square className="h-3 w-3 fill-current" aria-hidden="true" />
      {label}
    </button>
  );
}
