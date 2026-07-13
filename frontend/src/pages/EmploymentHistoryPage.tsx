import { useState } from "react";
import { Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Field, Select } from "@/components/ui/field";
import { Input, Textarea } from "@/components/ui/input";
import { Checkbox } from "@/components/ui/toggle";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Alert } from "@/components/ui/alert";
import { SectionHeading, ProgressCell, EmptyState } from "@/components/ui/misc";
import { Skeleton } from "@/components/ui/skeleton";
import { apiErrorMessage } from "@/lib/apiError";
import { useLatestResume } from "@/api/resume";
import { useEmploymentHistory, useSaveEmployment, useDeleteEmployment } from "@/api/employment";
import type { EmploymentHistory } from "@/types";

const EMPLOYMENT_TYPES = ["Full Time", "Internship", "Part Time", "Contract", "Freelance"];

function fmtLocation(r: EmploymentHistory) {
  return [r.city, r.state, r.country].filter(Boolean).join(", ");
}
function fmtDate(month: string | null, year: number | null) {
  return [month, year].filter(Boolean).join(" ");
}

function EmploymentForm({
  record,
  resumeId,
  recordsCount,
}: {
  record: EmploymentHistory | null;
  resumeId: number | undefined;
  recordsCount: number;
}) {
  const save = useSaveEmployment();
  const del = useDeleteEmployment();
  const [current, setCurrent] = useState(Boolean(record?.currently_work_here));

  const onSubmit = (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const fd = new FormData(e.currentTarget);
    const s = (k: string) => String(fd.get(k) ?? "");
    const splitList = (val: string) => val.split(",").map((x) => x.trim()).filter(Boolean);
    const startYear = s("start_year");
    const endYear = s("end_year");
    save.mutate({
      id: record?.id ?? null,
      payload: {
        resume_id: resumeId ?? record?.resume_id ?? null,
        // dashboard.py uses Python `or` (falsy fallback), so a stored display_order of 0
        // becomes len(records). Use || (not ??) to match that exactly.
        display_order: record?.display_order || recordsCount,
        company_name: s("company_name"),
        job_title: s("job_title"),
        city: s("city"),
        state: s("state"),
        country: s("country"),
        employment_type: s("employment_type"),
        start_month: s("start_month"),
        start_year: startYear ? Number(startYear) : null,
        end_month: current ? null : s("end_month"),
        end_year: current ? null : Number(endYear),
        currently_work_here: current,
        description: s("description"),
        skills_used: splitList(s("skills")),
        technologies_used: splitList(s("technologies")),
        confidence: record?.confidence || 100, // dashboard.py: `confidence or 100`
        source: record ? record.source || "Manual" : "Manual",
      },
    });
  };

  return (
    <>
      <form className="grid grid-cols-1 gap-4 md:grid-cols-2" onSubmit={onSubmit}>
        <Field label="Company"><Input name="company_name" defaultValue={record?.company_name ?? ""} /></Field>
        <Field label="Job title"><Input name="job_title" defaultValue={record?.job_title ?? ""} /></Field>
        <Field label="City"><Input name="city" defaultValue={record?.city ?? ""} /></Field>
        <Field label="State"><Input name="state" defaultValue={record?.state ?? ""} /></Field>
        <Field label="Country"><Input name="country" defaultValue={record?.country ?? ""} /></Field>
        <Field label="Employment type">
          <Select name="employment_type" options={EMPLOYMENT_TYPES} defaultValue={record?.employment_type ?? "Full Time"} />
        </Field>
        <Field label="Start month"><Input name="start_month" defaultValue={record?.start_month ?? ""} /></Field>
        <Field label="Start year"><Input name="start_year" type="number" min={1900} max={2100} defaultValue={record?.start_year ?? 2024} /></Field>
        <div className="md:col-span-2">
          <Checkbox checked={current} onCheckedChange={setCurrent} label="Currently work here" />
        </div>
        <Field label="End month"><Input name="end_month" disabled={current} defaultValue={record?.end_month ?? ""} /></Field>
        <Field label="End year"><Input name="end_year" type="number" min={1900} max={2100} disabled={current} defaultValue={record?.end_year ?? 2025} /></Field>
        <Field label="Description" className="md:col-span-2"><Textarea name="description" rows={3} defaultValue={record?.description ?? ""} /></Field>
        <Field label="Skills" className="md:col-span-2"><Input name="skills" defaultValue={(record?.skills_used ?? []).join(", ")} /></Field>
        <Field label="Technologies" className="md:col-span-2"><Input name="technologies" defaultValue={(record?.technologies_used ?? []).join(", ")} /></Field>
        <div className="flex flex-wrap items-center gap-2 md:col-span-2">
          <Button type="submit" variant="primary" disabled={save.isPending}>
            {save.isPending && <Loader2 className="size-4 animate-spin" />}
            Save employment
          </Button>
          {record && (
            <Button type="button" variant="destructive" disabled={del.isPending} onClick={() => del.mutate(record.id)}>
              {del.isPending && <Loader2 className="size-4 animate-spin" />}
              Delete selected employment
            </Button>
          )}
          {save.isSuccess && <span className="text-sm font-semibold text-green-700">Employment history saved</span>}
        </div>
      </form>
      {save.isError && <Alert variant="danger" className="mt-2">{apiErrorMessage("Employment history save", save.error)}</Alert>}
      {del.isError && <Alert variant="danger" className="mt-2">{apiErrorMessage("Employment history delete", del.error)}</Alert>}
    </>
  );
}

export function EmploymentHistoryPage() {
  const { data: resume } = useLatestResume();
  const { data, isLoading, isError, error } = useEmploymentHistory(resume?.id);
  const records = data ?? [];
  const recordOptions = ["Add new employment", ...records.map((r) => `${r.company_name ?? "Unknown"} | ${r.job_title ?? "Unknown"}`)];
  const [choice, setChoice] = useState(recordOptions[0]);
  const selectedIndex = recordOptions.indexOf(choice);
  const selected = selectedIndex > 0 ? records[selectedIndex - 1] ?? null : null;

  return (
    <div className="space-y-5">
      <SectionHeading title="Employment history" />

      {isLoading ? (
        <Skeleton className="h-40" />
      ) : isError ? (
        <Alert variant="danger">{apiErrorMessage("Load employment history", error)}</Alert>
      ) : records.length === 0 ? (
        <EmptyState message="No employment history extracted yet." />
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Company</TableHead>
              <TableHead>Job Title</TableHead>
              <TableHead>Location</TableHead>
              <TableHead>Start Date</TableHead>
              <TableHead>End Date</TableHead>
              <TableHead>Current Employer</TableHead>
              <TableHead>Confidence</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {records.map((r) => (
              <TableRow key={r.id}>
                <TableCell className="font-semibold text-ink">{r.company_name}</TableCell>
                <TableCell>{r.job_title}</TableCell>
                <TableCell>{fmtLocation(r)}</TableCell>
                <TableCell>{fmtDate(r.start_month, r.start_year)}</TableCell>
                <TableCell>{r.currently_work_here ? "Present" : fmtDate(r.end_month, r.end_year)}</TableCell>
                <TableCell>{r.currently_work_here ? "Yes" : "No"}</TableCell>
                <TableCell><ProgressCell value={r.confidence} suffix="%" /></TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}

      <Card>
        <CardHeader><CardTitle>Add or edit employment</CardTitle></CardHeader>
        <CardContent className="space-y-4">
          <Field label="Record">
            <Select options={recordOptions} value={choice} onChange={(e) => setChoice(e.target.value)} />
          </Field>
          {/* Remount the form when the selected record changes so defaults re-seed. */}
          <EmploymentForm key={selected?.id ?? "new"} record={selected} resumeId={resume?.id} recordsCount={records.length} />
        </CardContent>
      </Card>
    </div>
  );
}
