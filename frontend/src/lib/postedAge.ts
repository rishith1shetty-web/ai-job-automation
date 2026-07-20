// Visual emphasis tier for a job's raw posted-age text (display only: never used for
// sorting or data -- the backend order is authoritative and unchanged).
//
//   instant  "Just now" / "Moments ago" / seconds  -> strongest emphasis
//   minutes  "5 min ago"                           -> clearly highlighted
//   hours    "2 hours ago"                         -> normal emphasis
//   today    "Today"                               -> visible, weaker than minutes/hours
//   older    "Yesterday" / days / weeks / months / explicit dates -> neutral
//   unknown  missing or unrecognized               -> muted
export type PostedTier = "instant" | "minutes" | "hours" | "today" | "older" | "unknown";

const INSTANT_RE = /just\s+now|moments?\s+ago|\d+\s*sec(?:ond)?s?\s+ago/i;
const MINUTES_RE = /\d+\s*min(?:ute)?s?\s+ago/i;
const HOURS_RE = /\d+\s*(?:hrs?|hours?)\s+ago/i;
const TODAY_RE = /\btoday\b/i;
const OLDER_RE = /\byesterday\b|\d+\s*\+?\s*(?:days?|weeks?|months?)\s+ago|\d{4}-\d{2}-\d{2}|\b\d{4}\b/i;

export function postedTier(raw?: string | null): PostedTier {
  const text = (raw ?? "").trim();
  if (!text) return "unknown";
  if (INSTANT_RE.test(text)) return "instant";
  if (MINUTES_RE.test(text)) return "minutes";
  if (HOURS_RE.test(text)) return "hours";
  if (TODAY_RE.test(text)) return "today";
  if (OLDER_RE.test(text)) return "older";
  return "unknown";
}
