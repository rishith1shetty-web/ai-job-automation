import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Textarea } from "@/components/ui/input";
import { SectionHeading, ProgressCell, EmptyState } from "@/components/ui/misc";
import { useResumeIntelligence, useReasoning } from "@/api/evidence";

type Dict = Record<string, unknown>;
const arr = (v: unknown): Dict[] => (Array.isArray(v) ? (v as Dict[]) : []);
const str = (v: unknown): string => (v === null || v === undefined ? "" : String(v));
const num = (v: unknown): number => (typeof v === "number" ? v : 0);
const joinList = (v: unknown): string => (Array.isArray(v) ? v.map(String).join(", ") : "");

export function AIReasoningPage() {
  const intelligence = useResumeIntelligence();
  const reasoning = useReasoning();
  const intel = intelligence.data;
  const reas = reasoning.data;
  const intelAvailable = Boolean(intel?.available);
  const reasAvailable = Boolean(reas?.available);

  const ranking = arr(intel?.ranking_analysis);
  const explanations = arr(intel?.match_explanation);
  const explanation = explanations[0];
  const coverLetters = arr(intel?.cover_letter);
  const suggestionGroups = arr(intel?.resume_suggestions);
  const generatedGroups = arr(intel?.generated_answers);
  const questionReasoning = arr(reas?.question_reasoning);
  const semantic = reas?.semantic_matches;
  const jobDescription = reas?.job_description_analysis;

  if (intelligence.isLoading || reasoning.isLoading) {
    return <div className="h-40 animate-pulse rounded-lg border border-parchment-border bg-[#F3EDE3]" />;
  }

  return (
    <div className="space-y-5">
      <SectionHeading title="AI reasoning" />

      {intelAvailable && (
        <>
          <p className="text-xs text-[#8A8580]">Evidence: {str(intel?.folder)}</p>
          {ranking.length > 0 && (
            <div>
              <h3 className="mb-2 text-base font-bold text-ink">Resume Intelligence</h3>
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Job</TableHead>
                    <TableHead>Company</TableHead>
                    <TableHead>Overall Match</TableHead>
                    <TableHead>Semantic Match</TableHead>
                    <TableHead>Technical Match</TableHead>
                    <TableHead>Confidence</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {ranking.map((item, i) => {
                    const job = (item.job as Dict) ?? {};
                    const result = (item.result as Dict) ?? {};
                    return (
                      <TableRow key={i}>
                        <TableCell className="font-semibold text-ink">{str(job.title)}</TableCell>
                        <TableCell>{str(job.company)}</TableCell>
                        <TableCell><ProgressCell value={num(result.overall_match)} suffix="%" /></TableCell>
                        <TableCell><ProgressCell value={num(result.semantic_match)} suffix="%" /></TableCell>
                        <TableCell><ProgressCell value={num(result.technical_match)} suffix="%" /></TableCell>
                        <TableCell>{str(result.confidence)}</TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
            </div>
          )}

          {explanation && (
            <Card>
              <CardHeader><CardTitle>Match explanation</CardTitle></CardHeader>
              <CardContent className="space-y-3">
                <p className="text-sm text-[#57534E]">{str(explanation.overall_summary)}</p>
                <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
                  {([["Strengths", "strengths"], ["Weaknesses", "weaknesses"], ["Recommendations", "recommendations"]] as const).map(([title, key]) => (
                    <div key={title}>
                      <div className="mb-1 text-sm font-bold text-ink">{title}</div>
                      <ul className="list-inside list-disc space-y-0.5 text-sm text-[#57534E]">
                        {(Array.isArray(explanation[key]) ? (explanation[key] as unknown[]) : []).map((x, i) => (
                          <li key={i}>{String(x)}</li>
                        ))}
                      </ul>
                    </div>
                  ))}
                </div>
              </CardContent>
            </Card>
          )}

          {coverLetters[0] && (
            <Card>
              <CardHeader><CardTitle>Cover Letter Preview</CardTitle></CardHeader>
              <CardContent>
                {/* Display-only / no-op, matching Streamlit (never persisted). */}
                <Textarea rows={6} readOnly value={str(coverLetters[0].cover_letter)} />
              </CardContent>
            </Card>
          )}

          {suggestionGroups.length > 0 && (
            <div>
              <h3 className="mb-2 text-base font-bold text-ink">Resume Suggestions</h3>
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Suggestion</TableHead>
                    <TableHead>Why it helps</TableHead>
                    <TableHead>Source</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {suggestionGroups.flatMap((g, gi) =>
                    arr(g.suggestions).map((s, si) => (
                      <TableRow key={`${gi}-${si}`}>
                        <TableCell className="font-medium text-ink">{str(s.suggestion)}</TableCell>
                        <TableCell>{str(s.why_it_helps)}</TableCell>
                        <TableCell>{str(s.source)}</TableCell>
                      </TableRow>
                    )),
                  )}
                </TableBody>
              </Table>
            </div>
          )}

          {generatedGroups.length > 0 && (
            <div>
              <h3 className="mb-2 text-base font-bold text-ink">Generated AI Answers</h3>
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Prompt</TableHead>
                    <TableHead>Answer</TableHead>
                    <TableHead>Confidence</TableHead>
                    <TableHead>Grounding</TableHead>
                    <TableHead>Evidence</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {arr(generatedGroups[0].answers).map((a, i) => (
                    <TableRow key={i}>
                      <TableCell className="font-medium text-ink">{str(a.prompt)}</TableCell>
                      <TableCell className="max-w-sm text-xs">{str(a.answer)}</TableCell>
                      <TableCell>{str(a.confidence)}</TableCell>
                      <TableCell>{str(a.grounding_score)}</TableCell>
                      <TableCell>{joinList(a.evidence_used)}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          )}
        </>
      )}

      {reasAvailable ? (
        <>
          <p className="text-xs text-[#8A8580]">Evidence: {str(reas?.folder)}</p>
          {questionReasoning.length > 0 && (
            <div>
              <h3 className="mb-2 text-base font-bold text-ink">Question reasoning</h3>
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Question</TableHead>
                    <TableHead>Answer</TableHead>
                    <TableHead>Confidence</TableHead>
                    <TableHead>Reasoning Type</TableHead>
                    <TableHead>Summary</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {questionReasoning.map((q, i) => (
                    <TableRow key={i}>
                      <TableCell className="font-medium text-ink">{str(q.question_text)}</TableCell>
                      <TableCell>{str(q.selected_answer ?? q.answer)}</TableCell>
                      <TableCell>{str(q.confidence)}</TableCell>
                      <TableCell>{str(q.reasoning_type)}</TableCell>
                      <TableCell className="max-w-sm text-xs">{str(q.reasoning_summary ?? q.reason)}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          )}
          {jobDescription != null && (
            <div>
              <h3 className="mb-1 text-base font-bold text-ink">Job description metadata</h3>
              <pre className="max-h-72 overflow-auto rounded-md border border-parchment-border bg-[#FBF7F0] px-3 py-2 text-xs text-[#57534E]">
                {JSON.stringify(jobDescription, null, 2)}
              </pre>
            </div>
          )}
          {semantic != null && (
            <div>
              <h3 className="mb-1 text-base font-bold text-ink">Resume vs job description</h3>
              <pre className="max-h-72 overflow-auto rounded-md border border-parchment-border bg-[#FBF7F0] px-3 py-2 text-xs text-[#57534E]">
                {JSON.stringify(semantic, null, 2)}
              </pre>
            </div>
          )}
        </>
      ) : (
        !intelAvailable && <EmptyState message="No reasoning diagnostics have been recorded yet." />
      )}
    </div>
  );
}
