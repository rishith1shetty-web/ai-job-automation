import { Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Alert } from "@/components/ui/alert";
import { SectionHeading, ProgressCell } from "@/components/ui/misc";
import { apiErrorMessage } from "@/lib/apiError";
import { useCandidateProfile, useSaveCandidateProfile } from "@/api/resume";
import { useLatestResume } from "@/api/resume";
import type { CandidateProfile } from "@/types";

const FIELD_SOURCE_ROWS: [string, keyof CandidateProfile][] = [
  ["First name", "first_name"], ["Last name", "last_name"], ["Email", "email"],
  ["Mobile phone", "mobile_phone"], ["Phone country code", "phone_country_code"],
  ["Location", "current_location"], ["LinkedIn URL", "linkedin_url"], ["GitHub URL", "github_url"],
  ["Portfolio URL", "portfolio_url"], ["Current title", "current_job_title"], ["Current company", "current_company"],
];

function ResumeDetail() {
  const { data: resume } = useLatestResume();
  if (!resume) return <p className="text-sm text-[#8A8580]">Upload and analyze a resume to view the profile.</p>;
  const lists: [string, string[], string][] = [
    ["Skills", resume.skills, "No skills extracted"],
    ["Job titles", resume.job_titles, "No job titles extracted"],
    ["Technologies", resume.technologies, "No technologies extracted"],
    ["Domain expertise", resume.domain_expertise, "No domain expertise inferred"],
    ["Generated search keywords", resume.generated_search_keywords, ""],
  ];
  return (
    <div className="space-y-3">
      {lists.map(([title, values, empty]) => (
        <div key={title}>
          <div className="text-sm font-bold text-ink">{title}</div>
          <div className="text-sm text-[#57534E]">{values.length ? values.join(", ") : empty}</div>
        </div>
      ))}
      <div>
        <div className="mb-1 text-sm font-bold text-ink">Generated search query</div>
        <pre className="overflow-x-auto rounded-md border border-parchment-border bg-[#FBF7F0] px-3 py-2 text-xs text-brown-deep">
          {resume.generated_search_query ?? ""}
        </pre>
      </div>
      {(["education", "experience"] as const).map((k) => (
        <div key={k}>
          <div className="mb-1 text-sm font-bold capitalize text-ink">{k}</div>
          <pre className="max-h-64 overflow-auto rounded-md border border-parchment-border bg-[#FBF7F0] px-3 py-2 text-xs text-[#57534E]">
            {JSON.stringify(resume[k], null, 2)}
          </pre>
        </div>
      ))}
    </div>
  );
}

export function ProfilePage() {
  const { data: profile, isLoading } = useCandidateProfile();
  const save = useSaveCandidateProfile();

  const v = (k: keyof CandidateProfile) => (profile?.[k] as string) || "";

  const onSubmit = (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const fd = new FormData(e.currentTarget);
    const s = (k: string) => String(fd.get(k) ?? "");
    save.mutate({
      first_name: s("first_name"),
      last_name: s("last_name"),
      email: s("email"),
      mobile_phone: s("mobile_phone"),
      phone_country_code: s("phone_country_code"),
      current_location: s("current_location"),
      linkedin_url: s("linkedin_url"),
      portfolio_url: s("portfolio_url"),
      github_url: s("github_url"),
      current_job_title: s("current_job_title"),
      current_company: s("current_company"),
      professional_headline: s("professional_headline"),
    });
  };

  return (
    <div className="space-y-5">
      <SectionHeading title="Candidate contact profile" />

      <Card>
        <CardContent className="pt-4">
          {isLoading ? (
            <div className="h-64 animate-pulse rounded-lg bg-[#F3EDE3]" />
          ) : (
            // Re-seed defaults whenever the saved profile changes (after save/refetch).
            <form key={profile?.updated_at ?? "new"} className="grid grid-cols-1 gap-4 md:grid-cols-2" onSubmit={onSubmit}>
              <Field label="First name"><Input name="first_name" defaultValue={v("first_name")} /></Field>
              <Field label="Last name"><Input name="last_name" defaultValue={v("last_name")} /></Field>
              <Field label="Email address"><Input name="email" defaultValue={v("email")} /></Field>
              <Field label="Phone country code"><Input name="phone_country_code" placeholder="+1, +91" defaultValue={v("phone_country_code")} /></Field>
              <Field label="Mobile phone number"><Input name="mobile_phone" defaultValue={v("mobile_phone")} /></Field>
              <Field label="Current city/location"><Input name="current_location" placeholder="City, State/Country" defaultValue={v("current_location")} /></Field>
              <Field label="LinkedIn profile URL" className="md:col-span-2"><Input name="linkedin_url" defaultValue={v("linkedin_url")} /></Field>
              <Field label="Current job title"><Input name="current_job_title" defaultValue={v("current_job_title")} /></Field>
              <Field label="Current company"><Input name="current_company" defaultValue={v("current_company")} /></Field>
              <Field label="Professional headline" className="md:col-span-2"><Input name="professional_headline" defaultValue={v("professional_headline")} /></Field>
              <Field label="Portfolio URL optional"><Input name="portfolio_url" defaultValue={v("portfolio_url")} /></Field>
              <Field label="GitHub URL optional"><Input name="github_url" defaultValue={v("github_url")} /></Field>
              <div className="md:col-span-2 flex items-center gap-3">
                <Button type="submit" variant="primary" disabled={save.isPending}>
                  {save.isPending && <Loader2 className="size-4 animate-spin" />}
                  Save candidate profile
                </Button>
                {save.isSuccess && <span className="text-sm font-semibold text-green-700">Candidate profile saved</span>}
              </div>
              {save.isError && <Alert variant="danger" className="md:col-span-2">{apiErrorMessage("Candidate profile save", save.error)}</Alert>}
            </form>
          )}
        </CardContent>
      </Card>

      {profile?.is_complete ? (
        <Alert variant="success">Candidate profile is complete for standard Easy Apply contact fields.</Alert>
      ) : profile ? (
        <>
          <Alert variant="info">
            Candidate profile is optional. Missing fields are handled only if a specific Easy Apply form requires them.
          </Alert>
          {(profile.missing_required_after_apply ?? []).length > 0 && (
            <Alert variant="warn">
              Missing required fields from last apply attempt: {profile.missing_required_after_apply.join(", ")}
            </Alert>
          )}
        </>
      ) : null}

      {profile && (
        <div>
          <h3 className="mb-2 text-base font-bold text-ink">Auto-filled field sources</h3>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Field</TableHead>
                <TableHead>Value</TableHead>
                <TableHead>Source</TableHead>
                <TableHead>Confidence</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {FIELD_SOURCE_ROWS.map(([label, key]) => {
                const meta = profile.field_sources?.[key] ?? {};
                return (
                  <TableRow key={label}>
                    <TableCell className="font-medium text-ink">{label}</TableCell>
                    <TableCell>{(profile[key] as string) || "—"}</TableCell>
                    <TableCell>{meta.source || "—"}</TableCell>
                    <TableCell><ProgressCell value={meta.confidence ?? 0} suffix="%" /></TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
          {profile.professional_links && Object.keys(profile.professional_links).length > 0 && (
            <div className="mt-3">
              <h3 className="mb-1 text-base font-bold text-ink">Professional links</h3>
              <pre className="overflow-auto rounded-md border border-parchment-border bg-[#FBF7F0] px-3 py-2 text-xs text-[#57534E]">
                {JSON.stringify(profile.professional_links, null, 2)}
              </pre>
            </div>
          )}
        </div>
      )}

      <hr className="border-parchment-border" />
      <Card>
        <CardHeader><CardTitle>Resume detail</CardTitle></CardHeader>
        <CardContent><ResumeDetail /></CardContent>
      </Card>
    </div>
  );
}
