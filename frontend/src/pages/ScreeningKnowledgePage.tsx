import { useState } from "react";
import { Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Field, Select } from "@/components/ui/field";
import { Input, Textarea } from "@/components/ui/input";
import { Card, CardContent } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Alert } from "@/components/ui/alert";
import { SectionHeading, EmptyState } from "@/components/ui/misc";
import { apiErrorMessage } from "@/lib/apiError";
import { useLatestResume } from "@/api/resume";
import {
  useScreeningSettings,
  useSaveScreeningSettings,
  useScreeningKnowledge,
  useSaveScreeningAnswer,
  useDeleteScreeningAnswer,
} from "@/api/screening";
import type { ScreeningSetting } from "@/types";

const MODE_OPTIONS = ["Safe Mode", "Aggressive Mode"];
const NOTICE_OPTIONS = ["Immediate", "15 days", "30 days", "Custom"];
const CATEGORY_OPTIONS = ["salary", "skill_years", "yes_no", "skill", "education", "years", "location", "notice", "authorization", "general"];
const CURRENCY_OPTIONS = ["", "USD", "INR", "CAD", "GBP", "EUR"];
const CONFIDENCE_OPTIONS = ["High", "Medium", "Estimated / Low"];

function SettingsForm({ settings }: { settings: ScreeningSetting | undefined }) {
  const save = useSaveScreeningSettings();
  const savedMode = (settings?.mode ?? "safe").toLowerCase();
  const [mode, setMode] = useState(savedMode === "aggressive" ? "Aggressive Mode" : "Safe Mode");
  const initialNotice = NOTICE_OPTIONS.includes(settings?.notice_period_default ?? "")
    ? (settings?.notice_period_default as string)
    : "30 days";
  const [notice, setNotice] = useState(initialNotice);
  const [custom, setCustom] = useState(settings?.notice_period_default ?? "");

  const onSubmit = (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    save.mutate({
      mode: mode === "Aggressive Mode" ? "aggressive" : "safe",
      notice_period_default: notice === "Custom" ? custom : notice,
    });
  };

  return (
    <>
      <Card>
        <CardContent className="pt-4">
          <form className="grid grid-cols-1 gap-4 md:grid-cols-2" onSubmit={onSubmit}>
            <Field label="Screening Automation Mode">
              <Select options={MODE_OPTIONS} value={mode} onChange={(e) => setMode(e.target.value)} />
            </Field>
            <Field label="Notice Period Default">
              <Select options={NOTICE_OPTIONS} value={notice} onChange={(e) => setNotice(e.target.value)} />
              {notice === "Custom" && (
                <Input placeholder="Custom notice period" value={custom} onChange={(e) => setCustom(e.target.value)} />
              )}
            </Field>
            <div className="md:col-span-2 flex items-center gap-3">
              <Button type="submit" variant="primary" disabled={save.isPending}>
                {save.isPending && <Loader2 className="size-4 animate-spin" />}
                Save screening settings
              </Button>
              {save.isSuccess && <span className="text-sm font-semibold text-green-700">Screening settings saved</span>}
            </div>
            {save.isError && <Alert variant="danger" className="md:col-span-2">{apiErrorMessage("Screening settings save", save.error)}</Alert>}
          </form>
        </CardContent>
      </Card>

      {savedMode === "aggressive" ? (
        <Alert variant="warn">
          Aggressive Mode may answer unknown non-sensitive screening questions automatically. Dry Run is recommended for testing.
        </Alert>
      ) : (
        <Alert variant="info">Safe Mode leaves unknown screening questions for recruiter review.</Alert>
      )}
    </>
  );
}

function AnswerForm() {
  const { data: resume } = useLatestResume();
  const save = useSaveScreeningAnswer();

  const onSubmit = (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const fd = new FormData(e.currentTarget);
    const s = (k: string) => String(fd.get(k) ?? "");
    const currency = s("currency");
    save.mutate({
      question_text: s("question_text"),
      category: s("category"),
      answer: s("answer"),
      source: "Manual",
      confidence: s("confidence"),
      currency: currency || null, // "" becomes null, not empty string
      evidence: { note: s("evidence_used") },
      resume_id: resume?.id ?? null,
    });
  };

  return (
    <Card>
      <CardContent className="pt-4">
        <form key={save.isSuccess ? "saved" : "editing"} className="grid grid-cols-1 gap-4 md:grid-cols-2" onSubmit={onSubmit}>
          <Field label="Detected question" className="md:col-span-2">
            <Textarea name="question_text" rows={2} placeholder="Example: What is your expected CTC?" />
          </Field>
          <Field label="Category"><Select name="category" options={CATEGORY_OPTIONS} /></Field>
          <Field label="Answer selected"><Input name="answer" /></Field>
          <Field label="Currency"><Select name="currency" options={CURRENCY_OPTIONS} /></Field>
          <Field label="Confidence"><Select name="confidence" options={CONFIDENCE_OPTIONS} /></Field>
          <Field label="Evidence used" className="md:col-span-2">
            <Textarea name="evidence_used" rows={2} placeholder="Example: Recruiter confirmed expected salary for this candidate." />
          </Field>
          <div className="md:col-span-2 flex items-center gap-3">
            <Button type="submit" disabled={save.isPending}>
              {save.isPending && <Loader2 className="size-4 animate-spin" />}
              Save answer
            </Button>
            {save.isSuccess && <span className="text-sm font-semibold text-green-700">Saved screening answer for this candidate</span>}
          </div>
          {save.isError && <Alert variant="danger" className="md:col-span-2">{apiErrorMessage("Save screening answer", save.error)}</Alert>}
        </form>
      </CardContent>
    </Card>
  );
}

export function ScreeningKnowledgePage() {
  const { data: settings } = useScreeningSettings();
  const { data: answersData } = useScreeningKnowledge();
  const del = useDeleteScreeningAnswer();
  const answers = answersData ?? [];
  const [deleteChoice, setDeleteChoice] = useState("");

  const deleteOptions = ["", ...answers.map((a) => `${a.category} | ${a.question_text.slice(0, 80)}`)];
  const deleteId = (() => {
    const idx = deleteOptions.indexOf(deleteChoice);
    return idx > 0 ? answers[idx - 1].id : null;
  })();

  const skillYearRows = answers.filter((a) => a.category === "skill_years");

  return (
    <div className="space-y-5">
      <SectionHeading title="Screening automation" />

      <SettingsForm key={settings?.updated_at ?? "new"} settings={settings} />

      <h3 className="text-base font-bold text-ink">Saved candidate answers</h3>
      {answers.length === 0 ? (
        <EmptyState message="No saved screening answers yet." />
      ) : (
        <>
          {skillYearRows.length > 0 && (
            <div>
              <div className="mb-1 text-sm font-bold text-ink">Skill experience estimates</div>
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Skill</TableHead>
                    <TableHead>Estimated years</TableHead>
                    <TableHead>Source</TableHead>
                    <TableHead>Confidence</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {skillYearRows.map((a) => (
                    <TableRow key={a.id}>
                      <TableCell className="font-medium text-ink">
                        {(a.evidence?.normalized_skill as string) || a.question_text.replace(" years experience", "")}
                      </TableCell>
                      <TableCell>{a.answer}</TableCell>
                      <TableCell>{a.source}</TableCell>
                      <TableCell>{a.confidence}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          )}

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
              {answers.map((a) => (
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
            <Button variant="destructive" disabled={!deleteId || del.isPending} onClick={() => deleteId && del.mutate(deleteId, { onSuccess: () => setDeleteChoice("") })}>
              {del.isPending && <Loader2 className="size-4 animate-spin" />}
              Delete answer
            </Button>
          </div>
          {del.isError && <Alert variant="danger">{apiErrorMessage("Delete saved answer", del.error)}</Alert>}
        </>
      )}

      <h3 className="text-base font-bold text-ink">Save or update an answer</h3>
      <AnswerForm />
    </div>
  );
}
