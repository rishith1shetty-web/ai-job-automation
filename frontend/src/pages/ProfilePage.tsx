import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Alert } from "@/components/ui/alert";
import { SectionHeading, ProgressCell } from "@/components/ui/misc";
import { mockAction } from "@/lib/mock";
import { candidateProfile, resume } from "@/fixtures";

const FIELD_SOURCE_ROWS: [string, keyof typeof candidateProfile.field_sources][] = [
  ["First name", "first_name"], ["Last name", "last_name"], ["Email", "email"],
  ["Mobile phone", "mobile_phone"], ["Phone country code", "phone_country_code"],
  ["Location", "current_location"], ["LinkedIn URL", "linkedin_url"], ["GitHub URL", "github_url"],
  ["Portfolio URL", "portfolio_url"], ["Current title", "current_job_title"], ["Current company", "current_company"],
];

export function ProfilePage() {
  const p = candidateProfile;
  const [form, setForm] = useState({
    first_name: p.first_name ?? "", last_name: p.last_name ?? "", email: p.email ?? "",
    phone_country_code: p.phone_country_code ?? "", mobile_phone: p.mobile_phone ?? "",
    current_location: p.current_location ?? "", linkedin_url: p.linkedin_url ?? "",
    current_job_title: p.current_job_title ?? "", current_company: p.current_company ?? "",
    professional_headline: p.professional_headline ?? "", portfolio_url: p.portfolio_url ?? "",
    github_url: p.github_url ?? "",
  });
  const set = (k: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setForm((f) => ({ ...f, [k]: e.target.value }));

  return (
    <div className="space-y-5">
      <SectionHeading title="Candidate contact profile" />

      <Card>
        <CardContent className="pt-4">
          <form
            className="grid grid-cols-1 gap-4 md:grid-cols-2"
            onSubmit={(e) => { e.preventDefault(); mockAction("Save candidate profile"); }}
          >
            <Field label="First name"><Input value={form.first_name} onChange={set("first_name")} /></Field>
            <Field label="Last name"><Input value={form.last_name} onChange={set("last_name")} /></Field>
            <Field label="Email address"><Input value={form.email} onChange={set("email")} /></Field>
            <Field label="Phone country code"><Input placeholder="+1, +91" value={form.phone_country_code} onChange={set("phone_country_code")} /></Field>
            <Field label="Mobile phone number"><Input value={form.mobile_phone} onChange={set("mobile_phone")} /></Field>
            <Field label="Current city/location"><Input placeholder="City, State/Country" value={form.current_location} onChange={set("current_location")} /></Field>
            <Field label="LinkedIn profile URL" className="md:col-span-2"><Input value={form.linkedin_url} onChange={set("linkedin_url")} /></Field>
            <Field label="Current job title"><Input value={form.current_job_title} onChange={set("current_job_title")} /></Field>
            <Field label="Current company"><Input value={form.current_company} onChange={set("current_company")} /></Field>
            <Field label="Professional headline" className="md:col-span-2"><Input value={form.professional_headline} onChange={set("professional_headline")} /></Field>
            <Field label="Portfolio URL optional"><Input value={form.portfolio_url} onChange={set("portfolio_url")} /></Field>
            <Field label="GitHub URL optional"><Input value={form.github_url} onChange={set("github_url")} /></Field>
            <div className="md:col-span-2">
              <Button type="submit" variant="primary">Save candidate profile</Button>
            </div>
          </form>
        </CardContent>
      </Card>

      {p.is_complete ? (
        <Alert variant="success">Candidate profile is complete for standard Easy Apply contact fields.</Alert>
      ) : (
        <Alert variant="info">
          Candidate profile is optional. Missing fields are handled only if a specific Easy Apply form requires them.
        </Alert>
      )}

      {/* Auto-filled field sources */}
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
              const meta = p.field_sources[key] ?? {};
              const value = (p[key as keyof typeof p] as string) || "";
              return (
                <TableRow key={label}>
                  <TableCell className="font-medium text-ink">{label}</TableCell>
                  <TableCell>{value || "—"}</TableCell>
                  <TableCell>{meta.source || "—"}</TableCell>
                  <TableCell><ProgressCell value={meta.confidence ?? 0} suffix="%" /></TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      </div>

      {/* Read-only resume detail */}
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        {([
          ["Skills", resume.skills],
          ["Job titles", resume.job_titles],
          ["Technologies", resume.technologies],
          ["Domain expertise", resume.domain_expertise],
          ["Generated search keywords", resume.generated_search_keywords],
        ] as const).map(([title, values]) => (
          <Card key={title}>
            <CardHeader><CardTitle>{title}</CardTitle></CardHeader>
            <CardContent className="text-sm text-[#57534E]">
              {values.length ? values.join(", ") : `No ${title.toLowerCase()} extracted`}
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  );
}
