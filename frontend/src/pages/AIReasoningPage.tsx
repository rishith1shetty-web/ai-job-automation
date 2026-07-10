import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Textarea } from "@/components/ui/input";
import { SectionHeading, ProgressCell } from "@/components/ui/misc";
import { reasoning } from "@/fixtures";

export function AIReasoningPage() {
  const explanation = reasoning.match_explanation[0];
  return (
    <div className="space-y-5">
      <SectionHeading title="AI reasoning" subtitle={`Evidence: ${reasoning.folder}`} />

      {/* Ranking analysis */}
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
            {reasoning.ranking_analysis.map((item, i) => (
              <TableRow key={i}>
                <TableCell className="font-semibold text-ink">{item.job.title}</TableCell>
                <TableCell>{item.job.company}</TableCell>
                <TableCell><ProgressCell value={item.result.overall_match} suffix="%" /></TableCell>
                <TableCell><ProgressCell value={item.result.semantic_match} suffix="%" /></TableCell>
                <TableCell><ProgressCell value={item.result.technical_match} suffix="%" /></TableCell>
                <TableCell>{item.result.confidence}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>

      {/* Match explanation */}
      <Card>
        <CardHeader><CardTitle>Match explanation</CardTitle></CardHeader>
        <CardContent className="space-y-3">
          <p className="text-sm text-[#57534E]">{explanation.overall_summary}</p>
          <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
            {([
              ["Strengths", explanation.strengths],
              ["Weaknesses", explanation.weaknesses],
              ["Recommendations", explanation.recommendations],
            ] as const).map(([title, items]) => (
              <div key={title}>
                <div className="mb-1 text-sm font-bold text-ink">{title}</div>
                <ul className="list-inside list-disc space-y-0.5 text-sm text-[#57534E]">
                  {items.map((x) => <li key={x}>{x}</li>)}
                </ul>
              </div>
            ))}
          </div>
        </CardContent>
      </Card>

      {/* Cover letter preview (display only — never persisted, matching Streamlit) */}
      <Card>
        <CardHeader><CardTitle>Cover Letter Preview</CardTitle></CardHeader>
        <CardContent>
          <Textarea rows={6} readOnly value={reasoning.cover_letter[0].cover_letter} />
        </CardContent>
      </Card>

      {/* Question reasoning */}
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
            {reasoning.question_reasoning.map((q, i) => (
              <TableRow key={i}>
                <TableCell className="font-medium text-ink">{q.question_text}</TableCell>
                <TableCell>{q.selected_answer}</TableCell>
                <TableCell>{q.confidence}</TableCell>
                <TableCell>{q.reasoning_type}</TableCell>
                <TableCell className="max-w-sm text-xs">{q.reasoning_summary}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
    </div>
  );
}
