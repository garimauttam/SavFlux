import type { ReviewSection } from "../../hooks/useMultiReview";
export function reviewOutcome(section: ReviewSection): string {
  if (section.fallbackReason) return "Model unavailable · static only";
  if (section.status === "skipped")
    return section.findings?.length
      ? "Stopped · static findings only"
      : "Not reviewed";
  if (section.status === "error") return "Interrupted";
  if (section.status !== "complete")
    return section.status === "pending" ? "Queued" : "Reviewing";
  return section.tier === "static"
    ? "Static analysis"
    : "Model review complete";
}
