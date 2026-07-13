import { useState } from "react";
import { Field, Select } from "@/components/ui/field";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { SectionHeading, ProgressCell, EmptyState } from "@/components/ui/misc";
import { Skeleton } from "@/components/ui/skeleton";
import { useFormLearning, useFormLearningLatest } from "@/api/evidence";

type Dict = Record<string, unknown>;
const str = (v: unknown): string => (v === null || v === undefined ? "" : String(v));
const num = (v: unknown): number => (typeof v === "number" ? v : 0);
const joinList = (v: unknown): string => (Array.isArray(v) ? v.map(String).join(", ") : "");

export function FormLearningPage() {
  const learning = useFormLearning();
  const latest = useFormLearningLatest();
  const fields = learning.data ?? [];
  const [selectedField, setSelectedField] = useState("");

  const effectiveField = selectedField || str(fields[0]?.normalized_field);
  const selected = fields.find((f) => str(f.normalized_field) === effectiveField) ?? null;
  const history = selected ? (Array.isArray(selected.validation_history) ? (selected.validation_history as Dict[]) : []) : [];
  const historyCols = history.length > 0 ? Object.keys(history[0]) : [];

  return (
    <div className="space-y-4">
      <SectionHeading title="AI Form Learning" />
      {latest.data?.available && <p className="text-xs text-[#8A8580]">Latest evidence: {str(latest.data.folder)}</p>}

      {learning.isLoading ? (
        <Skeleton className="h-40" />
      ) : fields.length === 0 ? (
        <EmptyState message="No learned form fields yet. They will appear after application dry-runs or submissions process fields." />
      ) : (
        <>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Normalized Field</TableHead>
                <TableHead>Category</TableHead>
                <TableHead>Original Labels</TableHead>
                <TableHead>Companies Seen</TableHead>
                <TableHead>Success Rate</TableHead>
                <TableHead>Confidence</TableHead>
                <TableHead>Times Learned</TableHead>
                <TableHead>Times Used</TableHead>
                <TableHead>Last Value Source</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {fields.map((f, i) => (
                <TableRow key={i}>
                  <TableCell className="font-semibold text-ink">{str(f.normalized_field)}</TableCell>
                  <TableCell>{str(f.field_category)}</TableCell>
                  <TableCell>{joinList(f.original_labels)}</TableCell>
                  <TableCell>{joinList(f.companies_seen)}</TableCell>
                  <TableCell><ProgressCell value={num(f.success_rate)} suffix="%" /></TableCell>
                  <TableCell>{str(f.confidence)}</TableCell>
                  <TableCell className="tabular-nums">{str(f.times_seen)}</TableCell>
                  <TableCell className="tabular-nums">{str(f.times_used)}</TableCell>
                  <TableCell>{str(f.last_successful_value_source)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>

          <Field label="Validation history" className="max-w-md">
            <Select
              options={fields.map((f) => str(f.normalized_field))}
              value={effectiveField}
              onChange={(e) => setSelectedField(e.target.value)}
            />
          </Field>

          {history.length > 0 ? (
            <Table>
              <TableHeader>
                <TableRow>
                  {historyCols.map((c) => (
                    <TableHead key={c} className="capitalize">{c.replace(/_/g, " ")}</TableHead>
                  ))}
                </TableRow>
              </TableHeader>
              <TableBody>
                {history.map((h, i) => (
                  <TableRow key={i}>
                    {historyCols.map((c) => (
                      <TableCell key={c}>{str(h[c])}</TableCell>
                    ))}
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          ) : (
            <EmptyState message="No validation failures recorded for this learned field." />
          )}
        </>
      )}
    </div>
  );
}
