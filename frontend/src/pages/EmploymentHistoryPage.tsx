import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Field, Select } from "@/components/ui/field";
import { Input, Textarea } from "@/components/ui/input";
import { Checkbox } from "@/components/ui/toggle";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { SectionHeading, ProgressCell, EmptyState } from "@/components/ui/misc";
import { mockAction } from "@/lib/mock";
import { employmentHistory } from "@/fixtures";
import type { EmploymentHistory } from "@/types";

const EMPLOYMENT_TYPES = ["Full Time", "Internship", "Part Time", "Contract", "Freelance"];

function fmtLocation(r: EmploymentHistory) {
  return [r.city, r.state, r.country].filter(Boolean).join(", ");
}
function fmtDate(month: string | null, year: number | null) {
  return [month, year].filter(Boolean).join(" ");
}

export function EmploymentHistoryPage() {
  const records = employmentHistory;
  const recordOptions = ["Add new employment", ...records.map((r) => `${r.company_name ?? "Unknown"} | ${r.job_title ?? "Unknown"}`)];
  const [choice, setChoice] = useState(recordOptions[0]);
  const selected = choice === "Add new employment" ? null : records[recordOptions.indexOf(choice) - 1];
  const [current, setCurrent] = useState(Boolean(selected?.currently_work_here));

  return (
    <div className="space-y-5">
      <SectionHeading title="Employment history" />

      {records.length === 0 ? (
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
            <Select options={recordOptions} value={choice} onChange={(e) => { setChoice(e.target.value); }} />
          </Field>
          <form
            className="grid grid-cols-1 gap-4 md:grid-cols-2"
            onSubmit={(e) => { e.preventDefault(); mockAction("Save employment"); }}
          >
            <Field label="Company"><Input defaultValue={selected?.company_name ?? ""} /></Field>
            <Field label="Job title"><Input defaultValue={selected?.job_title ?? ""} /></Field>
            <Field label="City"><Input defaultValue={selected?.city ?? ""} /></Field>
            <Field label="State"><Input defaultValue={selected?.state ?? ""} /></Field>
            <Field label="Country"><Input defaultValue={selected?.country ?? ""} /></Field>
            <Field label="Employment type">
              <Select options={EMPLOYMENT_TYPES} defaultValue={selected?.employment_type ?? "Full Time"} />
            </Field>
            <Field label="Start month"><Input defaultValue={selected?.start_month ?? ""} /></Field>
            <Field label="Start year"><Input type="number" min={1900} max={2100} defaultValue={selected?.start_year ?? 2024} /></Field>
            <div className="md:col-span-2">
              <Checkbox checked={current} onCheckedChange={setCurrent} label="Currently work here" />
            </div>
            <Field label="End month"><Input disabled={current} defaultValue={selected?.end_month ?? ""} /></Field>
            <Field label="End year"><Input type="number" min={1900} max={2100} disabled={current} defaultValue={selected?.end_year ?? 2025} /></Field>
            <Field label="Description" className="md:col-span-2"><Textarea rows={3} defaultValue={selected?.description ?? ""} /></Field>
            <Field label="Skills" className="md:col-span-2"><Input defaultValue={(selected?.skills_used ?? []).join(", ")} /></Field>
            <Field label="Technologies" className="md:col-span-2"><Input defaultValue={(selected?.technologies_used ?? []).join(", ")} /></Field>
            <div className="flex gap-2 md:col-span-2">
              <Button type="submit" variant="primary">Save employment</Button>
              {selected && (
                <Button variant="destructive" onClick={() => mockAction("Delete selected employment")}>
                  Delete selected employment
                </Button>
              )}
            </div>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
