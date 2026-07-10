import { useState } from "react";
import { Field, Select } from "@/components/ui/field";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { SectionHeading, ProgressCell, EmptyState } from "@/components/ui/misc";
import { formLearning } from "@/fixtures";

export function FormLearningPage() {
  const fields = formLearning;
  const [selectedField, setSelectedField] = useState(fields[0]?.normalized_field ?? "");
  const selected = fields.find((f) => f.normalized_field === selectedField) ?? null;

  return (
    <div className="space-y-5">
      <SectionHeading title="AI Form Learning" />
      {fields.length === 0 ? (
        <EmptyState message="No learned form fields yet. They will appear after application dry-runs or submissions process fields." />
      ) : (
        <>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Normalized Field</TableHead>
                <TableHead>Category</TableHead>
                <TableHead>Companies Seen</TableHead>
                <TableHead>Success Rate</TableHead>
                <TableHead>Confidence</TableHead>
                <TableHead>Times Learned</TableHead>
                <TableHead>Times Used</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {fields.map((f) => (
                <TableRow key={f.normalized_field}>
                  <TableCell className="font-semibold text-ink">{f.normalized_field}</TableCell>
                  <TableCell>{f.field_category}</TableCell>
                  <TableCell>{f.companies_seen.join(", ")}</TableCell>
                  <TableCell><ProgressCell value={f.success_rate} suffix="%" /></TableCell>
                  <TableCell>{f.confidence}</TableCell>
                  <TableCell className="tabular-nums">{f.times_seen}</TableCell>
                  <TableCell className="tabular-nums">{f.times_used}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>

          <Field label="Validation history" className="max-w-md">
            <Select
              options={fields.map((f) => f.normalized_field)}
              value={selectedField}
              onChange={(e) => setSelectedField(e.target.value)}
            />
          </Field>

          {selected && selected.validation_history.length > 0 ? (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Company</TableHead>
                  <TableHead>Label</TableHead>
                  <TableHead>Outcome</TableHead>
                  <TableHead>Message</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {selected.validation_history.map((h, i) => (
                  <TableRow key={i}>
                    <TableCell>{h.company}</TableCell>
                    <TableCell>{h.label}</TableCell>
                    <TableCell>{h.outcome}</TableCell>
                    <TableCell>{h.message}</TableCell>
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
