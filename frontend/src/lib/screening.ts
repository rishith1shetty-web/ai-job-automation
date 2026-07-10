import type { ScreeningDecision } from "@/types";

// Faithful port of dashboard.py's extract_screening_decisions(): flatten screening
// decisions from an application record for display. Prefers persisted evidence_sources;
// otherwise reads the run_log (LinkedIn emits a screening_questions_answered event with
// an answers list; Dice emits per-field field_planned entries). No decision logic of its
// own — it only reshapes what the backend already returned.
type Dict = Record<string, unknown>;

const str = (v: unknown): string | null => (v === null || v === undefined ? null : String(v));
const conf = (v: unknown): string | number | null =>
  typeof v === "number" || typeof v === "string" ? v : null;

export function extractScreeningDecisions(record: {
  evidence_sources?: unknown;
  run_log?: unknown;
}): ScreeningDecision[] {
  const decisions: ScreeningDecision[] = [];

  const sources = Array.isArray(record.evidence_sources) ? (record.evidence_sources as Dict[]) : [];
  for (const source of sources) {
    const answer = source.answer;
    decisions.push({
      question: str(source.question),
      answer: str(answer),
      source: str(source.source),
      confidence: conf(source.confidence),
      reasoning: str(source.reasoning),
      needs_review: answer === null || answer === undefined || answer === "",
    });
  }
  if (decisions.length > 0) return decisions;

  const runLog = Array.isArray(record.run_log) ? (record.run_log as Dict[]) : [];
  for (const event of runLog) {
    if (event.event === "screening_questions_answered") {
      const answers = Array.isArray(event.answers) ? (event.answers as Dict[]) : [];
      for (const a of answers) {
        const selected = a.selected_answer ?? a.answer;
        decisions.push({
          question: str(a.question_text ?? a.question),
          answer: str(selected),
          source: str(a.source),
          confidence: conf(a.confidence),
          reasoning: str(a.reason ?? a.reasoning_summary),
          needs_review: !selected,
        });
      }
    } else if (event.step === "field_planned" && event.category) {
      const action = event.action;
      if (action === "resume_upload") continue;
      decisions.push({
        question: str(event.label),
        answer: str(event.answer),
        source: str(event.source),
        confidence: conf(event.confidence),
        reasoning: str(event.reason),
        needs_review: action === "needs_review",
      });
    }
  }
  return decisions;
}
