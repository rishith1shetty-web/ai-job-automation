import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { SectionHeading, EmptyState } from "@/components/ui/misc";
import { generalizedForm } from "@/fixtures";

export function GeneralizedFormEnginePage() {
  return (
    <div className="space-y-4">
      <SectionHeading
        title="Generalized Form Engine"
        subtitle="Detects field meaning and fill strategy only. Answers still come from the unified decision pipeline."
      />
      {generalizedForm.length === 0 ? (
        <EmptyState message="No generalized form metadata stored yet." />
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Portal</TableHead>
              <TableHead>Field label</TableHead>
              <TableHead>Normalized field</TableHead>
              <TableHead>Control type</TableHead>
              <TableHead>Fill strategy</TableHead>
              <TableHead>Success count</TableHead>
              <TableHead>Failure count</TableHead>
              <TableHead>Last validation message</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {generalizedForm.map((f, i) => (
              <TableRow key={i}>
                <TableCell className="capitalize">{f.portal}</TableCell>
                <TableCell className="font-medium text-ink">{f.original_field_label}</TableCell>
                <TableCell>{f.normalized_field_name}</TableCell>
                <TableCell>{f.detected_control_type}</TableCell>
                <TableCell>{f.recommended_fill_strategy}</TableCell>
                <TableCell className="tabular-nums">{f.times_successful}</TableCell>
                <TableCell className="tabular-nums">{f.times_failed}</TableCell>
                <TableCell className="max-w-sm text-xs">{f.last_validation_message}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </div>
  );
}
