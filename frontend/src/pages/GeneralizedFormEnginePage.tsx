import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { SectionHeading, EmptyState } from "@/components/ui/misc";
import { useGeneralizedForm, useGeneralizedFormLatest } from "@/api/evidence";

type Dict = Record<string, unknown>;
const str = (v: unknown): string => (v === null || v === undefined ? "" : String(v));

export function GeneralizedFormEnginePage() {
  const metadata = useGeneralizedForm();
  const latest = useGeneralizedFormLatest();
  const fields = metadata.data ?? [];
  const detected = latest.data?.available && Array.isArray(latest.data.detected_fields)
    ? (latest.data.detected_fields as Dict[])
    : [];
  const detectedCols = detected.length > 0 ? Object.keys(detected[0]) : [];
  const pipeline = latest.data?.pipeline_integration;

  return (
    <div className="space-y-4">
      <SectionHeading
        title="Generalized Form Engine"
        subtitle="Detects field meaning and fill strategy only. Answers still come from the unified decision pipeline."
      />

      {metadata.isLoading ? (
        <div className="h-40 animate-pulse rounded-lg border border-parchment-border bg-[#F3EDE3]" />
      ) : fields.length === 0 ? (
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
            {fields.map((f, i) => (
              <TableRow key={i}>
                <TableCell className="capitalize">{str(f.portal)}</TableCell>
                <TableCell className="font-medium text-ink">{str(f.original_field_label)}</TableCell>
                <TableCell>{str(f.normalized_field_name)}</TableCell>
                <TableCell>{str(f.detected_control_type)}</TableCell>
                <TableCell>{str(f.recommended_fill_strategy)}</TableCell>
                <TableCell className="tabular-nums">{str(f.times_successful)}</TableCell>
                <TableCell className="tabular-nums">{str(f.times_failed)}</TableCell>
                <TableCell className="max-w-sm text-xs">{str(f.last_validation_message)}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}

      {latest.data?.available && (
        <>
          <p className="text-xs text-[#8A8580]">Latest evidence: {str(latest.data.folder)}</p>
          {detected.length > 0 && (
            <div>
              <h3 className="mb-2 text-base font-bold text-ink">Detected fields</h3>
              <Table>
                <TableHeader>
                  <TableRow>
                    {detectedCols.map((c) => (
                      <TableHead key={c} className="capitalize">{c.replace(/_/g, " ")}</TableHead>
                    ))}
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {detected.map((d, i) => (
                    <TableRow key={i}>
                      {detectedCols.map((c) => (
                        <TableCell key={c} className="max-w-xs truncate text-xs">{str(d[c])}</TableCell>
                      ))}
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          )}
          {pipeline != null && (
            <div>
              <h3 className="mb-1 text-base font-bold text-ink">Pipeline integration</h3>
              <pre className="max-h-72 overflow-auto rounded-md border border-parchment-border bg-[#FBF7F0] px-3 py-2 text-xs text-[#57534E]">
                {JSON.stringify(pipeline, null, 2)}
              </pre>
            </div>
          )}
        </>
      )}
    </div>
  );
}
