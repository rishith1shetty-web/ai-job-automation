import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Field, Select } from "@/components/ui/field";
import { Input, Textarea } from "@/components/ui/input";
import { Card, CardContent } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Alert } from "@/components/ui/alert";
import { SectionHeading, EmptyState } from "@/components/ui/misc";
import { mockAction } from "@/lib/mock";
import { screeningAnswers, screeningSetting } from "@/fixtures";

const MODE_OPTIONS = ["Safe Mode", "Aggressive Mode"];
const NOTICE_OPTIONS = ["Immediate", "15 days", "30 days", "Custom"];
const CATEGORY_OPTIONS = ["salary", "skill_years", "yes_no", "skill", "education", "years", "location", "notice", "authorization", "general"];
const CURRENCY_OPTIONS = ["", "USD", "INR", "CAD", "GBP", "EUR"];
const CONFIDENCE_OPTIONS = ["High", "Medium", "Estimated / Low"];

export function ScreeningKnowledgePage() {
  const currentMode = screeningSetting.mode === "aggressive" ? "Aggressive Mode" : "Safe Mode";
  const [mode, setMode] = useState(currentMode);
  const [notice, setNotice] = useState(
    NOTICE_OPTIONS.includes(screeningSetting.notice_period_default ?? "") ? (screeningSetting.notice_period_default as string) : "30 days",
  );
  const deleteOptions = ["", ...screeningAnswers.map((a) => `${a.category} | ${a.question_text.slice(0, 80)}`)];
  const [deleteChoice, setDeleteChoice] = useState("");

  return (
    <div className="space-y-5">
      <SectionHeading title="Screening automation" />

      {/* Settings form */}
      <Card>
        <CardContent className="pt-4">
          <form className="grid grid-cols-1 gap-4 md:grid-cols-2" onSubmit={(e) => { e.preventDefault(); mockAction("Save screening settings"); }}>
            <Field label="Screening Automation Mode">
              <Select options={MODE_OPTIONS} value={mode} onChange={(e) => setMode(e.target.value)} />
            </Field>
            <Field label="Notice Period Default">
              <Select options={NOTICE_OPTIONS} value={notice} onChange={(e) => setNotice(e.target.value)} />
              {notice === "Custom" && <Input placeholder="Custom notice period" />}
            </Field>
            <div className="md:col-span-2">
              <Button type="submit" variant="primary">Save screening settings</Button>
            </div>
          </form>
        </CardContent>
      </Card>

      {mode === "Aggressive Mode" ? (
        <Alert variant="warn">
          Aggressive Mode may answer unknown non-sensitive screening questions automatically. Dry Run is recommended for testing.
        </Alert>
      ) : (
        <Alert variant="info">Safe Mode leaves unknown screening questions for recruiter review.</Alert>
      )}

      {/* Saved answers */}
      <h3 className="text-base font-bold text-ink">Saved candidate answers</h3>
      {screeningAnswers.length === 0 ? (
        <EmptyState message="No saved screening answers yet." />
      ) : (
        <>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Category</TableHead>
                <TableHead>Question</TableHead>
                <TableHead>Answer</TableHead>
                <TableHead>Source</TableHead>
                <TableHead>Confidence</TableHead>
                <TableHead>Currency</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {screeningAnswers.map((a) => (
                <TableRow key={a.id}>
                  <TableCell className="capitalize">{a.category.replace("_", " ")}</TableCell>
                  <TableCell className="font-medium text-ink">{a.question_text}</TableCell>
                  <TableCell>{a.answer}</TableCell>
                  <TableCell>{a.source}</TableCell>
                  <TableCell>{a.confidence}</TableCell>
                  <TableCell>{a.currency ?? "—"}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>

          <div className="flex flex-wrap items-end gap-3">
            <Field label="Delete saved answer" className="min-w-[280px] flex-1" hint="Optional: remove a saved candidate-specific answer.">
              <Select options={deleteOptions} value={deleteChoice} onChange={(e) => setDeleteChoice(e.target.value)} />
            </Field>
            <Button variant="destructive" disabled={!deleteChoice} onClick={() => mockAction("Delete answer")}>
              Delete answer
            </Button>
          </div>
        </>
      )}

      {/* Save or update an answer */}
      <h3 className="text-base font-bold text-ink">Save or update an answer</h3>
      <Card>
        <CardContent className="pt-4">
          <form className="grid grid-cols-1 gap-4 md:grid-cols-2" onSubmit={(e) => { e.preventDefault(); mockAction("Save answer"); }}>
            <Field label="Detected question" className="md:col-span-2">
              <Textarea rows={2} placeholder="Example: What is your expected CTC?" />
            </Field>
            <Field label="Category"><Select options={CATEGORY_OPTIONS} /></Field>
            <Field label="Answer selected"><Input /></Field>
            <Field label="Currency"><Select options={CURRENCY_OPTIONS} /></Field>
            <Field label="Confidence"><Select options={CONFIDENCE_OPTIONS} /></Field>
            <Field label="Evidence used" className="md:col-span-2">
              <Textarea rows={2} placeholder="Example: Recruiter confirmed expected salary for this candidate." />
            </Field>
            <div className="md:col-span-2">
              <Button type="submit">Save answer</Button>
            </div>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
