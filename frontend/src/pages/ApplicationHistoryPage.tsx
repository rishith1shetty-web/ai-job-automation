import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Alert } from "@/components/ui/alert";
import { SectionHeading, StatusBadge, EmptyState } from "@/components/ui/misc";
import { Skeleton } from "@/components/ui/skeleton";
import { apiErrorMessage } from "@/lib/apiError";
import { useLatestResume } from "@/api/resume";
import { usePortalStatus } from "@/api/portal";
import { useApplications } from "@/api/applications";
import { useSession } from "@/store/session";

export function ApplicationHistoryPage() {
  const { data: resume } = useLatestResume();
  const { data: portalStatus } = usePortalStatus();
  const { selectedBatchId } = useSession();
  const { data, isLoading, isError, error } = useApplications(
    resume?.id,
    selectedBatchId,
    portalStatus?.active_portal,
  );
  const applications = data ?? [];

  return (
    <div className="space-y-4">
      <SectionHeading title="Application history" subtitle="Every apply attempt recorded for the active portal." />
      {isLoading ? (
        <Skeleton className="h-40" />
      ) : isError ? (
        <Alert variant="danger">{apiErrorMessage("Load application history", error)}</Alert>
      ) : applications.length === 0 ? (
        <EmptyState message="No application history yet." />
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Job title</TableHead>
              <TableHead>Company</TableHead>
              <TableHead>Portal</TableHead>
              <TableHead>Status</TableHead>
              <TableHead>Dry run</TableHead>
              <TableHead>Last step</TableHead>
              <TableHead>Date</TableHead>
              <TableHead>Job URL</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {applications.map((a) => (
              <TableRow key={a.id}>
                <TableCell className="font-semibold text-ink">{a.job_title}</TableCell>
                <TableCell>{a.company}</TableCell>
                <TableCell className="capitalize">{a.portal}</TableCell>
                <TableCell><StatusBadge status={a.status} /></TableCell>
                <TableCell>{a.dry_run ? "Yes" : "No"}</TableCell>
                <TableCell>{(a.current_step ?? "—").replace(/_/g, " ")}</TableCell>
                <TableCell className="tabular-nums">{a.application_date.slice(0, 19).replace("T", " ")}</TableCell>
                <TableCell>
                  <a href={a.job_url} target="_blank" rel="noreferrer" className="font-semibold text-gold-text hover:underline">
                    Open
                  </a>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </div>
  );
}
