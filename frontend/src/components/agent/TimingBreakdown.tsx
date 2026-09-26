/**
 * TimingBreakdown.tsx — a review's cost, split into the parts that can be fixed.
 *
 * One total is a statistic; the split is a decision. "3.9 s" tells a reader nothing
 * about what to do next, while "plan 40 ms · context 810 ms · model 3.05 s (1 call)"
 * says the model is the wall, and "…but 900 ms of it was queue wait" says the
 * opposite. Both sentences come off the same markers, which is why the component
 * takes timings rather than fetching or parsing anything itself.
 *
 * The bar is decoration (`aria-hidden`): every segment is also a row with its number
 * and its share, so the information survives a screen reader, a narrow window, and a
 * colour-blind reader. A stage that was never measured is not rendered at all — see
 * the note in `lib/timings.ts` about the difference between 0 ms and no data.
 */

import { Clock, Gauge } from "lucide-react";
import { formatDuration } from "../../lib/stream";
import { stagesFor, type ReviewTimings } from "../../lib/timings";

const TONE: Record<string, string> = {
  plan: "bg-cyan-600",
  context: "bg-blue-500",
  analysis: "bg-emerald-500",
  model: "bg-pink-500",
  files: "bg-pink-500",
  review: "bg-pink-500",
  investigate: "bg-violet-500",
};

export interface TimingBreakdownProps {
  timings: ReviewTimings;
  /** Wall time since the reader pressed the button — a client number, so it is marked. */
  liveMs?: number | null;
  running?: boolean;
}

export function TimingBreakdown({ timings, liveMs = null, running = false }: TimingBreakdownProps) {
  const stages = stagesFor(timings);
  const measured = stages.reduce((sum, stage) => sum + stage.ms, 0);
  if (!stages.length && !running) return null;

  return (
    <section
      aria-label="Where the review time went"
      className="rounded-lg border border-gray-800 bg-gray-900/60 px-3 py-2.5"
    >
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <Gauge className="h-3.5 w-3.5 shrink-0 text-gray-500" />
        <h3 className="text-[11px] font-semibold uppercase tracking-wide text-gray-400">
          Where the time went
        </h3>
        <span className="ml-auto font-mono text-[10.5px] text-gray-500">
          {measured > 0 ? (
            <>
              {formatDuration(measured)}
              <span className="text-gray-600"> measured here</span>
            </>
          ) : null}
          {running && liveMs !== null && (
            <span className={measured > 0 ? "ml-2" : ""} title="Wall time since you started the review, measured by the browser">
              <Clock className="mr-1 inline h-3 w-3" />+{formatDuration(liveMs)} elapsed
            </span>
          )}
        </span>
      </div>

      {stages.length > 0 && (
        <>
          <div aria-hidden className="mt-2 flex h-1.5 w-full overflow-hidden rounded-full bg-gray-800">
            {stages.map((stage) => (
              <span
                key={stage.key}
                className={`h-full ${TONE[stage.key] ?? "bg-gray-500"}`}
                style={{ width: `${(stage.ms / measured) * 100}%` }}
              />
            ))}
          </div>

          <ul role="list" aria-label="Measured stages" className="mt-2 space-y-1">
            {stages.map((stage) => (
              <li key={stage.key} className="flex flex-wrap items-baseline gap-x-2 text-[11px]">
                <span className={`h-1.5 w-1.5 shrink-0 self-center rounded-full ${TONE[stage.key] ?? "bg-gray-500"}`} />
                <span className="text-gray-200">{stage.label}</span>
                {stage.hint && <span className="text-[10px] text-gray-600">{stage.hint}</span>}
                <span className="ml-auto font-mono text-gray-400">{formatDuration(stage.ms)}</span>
                <span className="w-9 text-right font-mono text-[10px] text-gray-600">
                  {Math.round((stage.ms / measured) * 100)}%
                </span>
              </li>
            ))}
          </ul>
        </>
      )}

      {timings.slowest && (
        <p className="mt-2 text-[10.5px] text-gray-500">
          Slowest file <span className="font-mono text-gray-300">{timings.slowest.file}</span>
          {timings.slowest.llmMs !== null && <> · model {formatDuration(timings.slowest.llmMs)}</>}
          {timings.slowest.waitMs !== null && (
            <> · queued {formatDuration(timings.slowest.waitMs)}</>
          )}
        </p>
      )}

      {timings.files.length > 1 && (
        // No second truncation here: the reducer already keeps the expensive end,
        // and cutting the tail again hid exactly the rows that explain a fast run.
        <ul role="list" aria-label="Slowest files" className="mt-1.5 space-y-0.5">
          {timings.files.map((entry) => (
            <li key={entry.file} className="flex items-baseline gap-2 text-[10.5px] text-gray-600">
              <span className="min-w-0 flex-1 truncate font-mono text-gray-500" title={entry.file}>
                {entry.file}
              </span>
              {/* No data and zero data are different facts: a cached file has no model
                  time at all, and printing 0 ms would report an instant answer. */}
              <span className="font-mono">
                {entry.cached
                  ? "cache"
                  : entry.llmMs === null
                    ? "no model call"
                    : `model ${formatDuration(entry.llmMs)}`}
              </span>
              {entry.waitMs !== null && <span className="font-mono">wait {formatDuration(entry.waitMs)}</span>}
            </li>
          ))}
        </ul>
      )}

      {running && !stages.length && (
        <p className="mt-1.5 text-[10.5px] text-gray-600">
          No stage has finished yet — the split appears as each one reports.
        </p>
      )}
    </section>
  );
}

export default TimingBreakdown;
