import { useState } from "react";
import { UploadCloud, Loader2 } from "lucide-react";
import { Logo } from "@/components/ui/Logo";
import { Button } from "@/components/ui/button";
import { Alert } from "@/components/ui/alert";
import { cn } from "@/lib/utils";
import { apiErrorMessage } from "@/lib/apiError";
import { useCandidateProfile, useUploadResume } from "@/api/resume";
import {
  usePortalStatus,
  useSwitchPortal,
  useConnectLinkedIn,
  useDisconnectLinkedIn,
  useConnectDice,
  useDisconnectDice,
} from "@/api/portal";
import type { Portal } from "@/types";

function SectionLabel({ children }: { children: string }) {
  return (
    <div className="mt-1.5 border-t border-[#E3D9CB] pt-3.5 text-[.68rem] font-extrabold uppercase tracking-[.12em] text-brown-deep first:mt-0 first:border-t-0 first:pt-0">
      {children}
    </div>
  );
}

function PortalCard({ portal, active, onSelect }: { portal: Portal; active: boolean; onSelect: () => void }) {
  const label = portal === "linkedin" ? "LinkedIn" : "Dice";
  return (
    <button
      type="button"
      onClick={onSelect}
      className={cn(
        "flex w-full items-center justify-between rounded-[11px] border px-3.5 py-2.5 text-left text-[.92rem] font-bold transition-all duration-200",
        active
          ? "border-[1.5px] border-gold bg-gradient-to-br from-[#FEF3E2] to-[#FDE8C7] text-brown-deep shadow-[0_2px_10px_rgba(217,119,6,.2)]"
          : "border-[#E3D9CB] bg-white text-[#4B5563] shadow-[0_1px_2px_rgba(62,46,30,.05)] hover:-translate-y-px hover:border-gold hover:bg-parchment-amber",
      )}
    >
      {label}
      {active && (
        <span className="text-[.68rem] font-extrabold uppercase tracking-[.06em] text-gold-text">Active</span>
      )}
    </button>
  );
}

export function Sidebar() {
  const { data: status } = usePortalStatus(); // LIVE: GET /portal/active
  const activePortal: Portal = status?.active_portal ?? "linkedin";
  const linkedinConnected = Boolean(status?.linkedin_connected);
  const diceConnected = Boolean(status?.dice_connected);

  const profile = useCandidateProfile();
  const upload = useUploadResume();
  const switchPortal = useSwitchPortal();
  const connectLinkedIn = useConnectLinkedIn();
  const disconnectLinkedIn = useDisconnectLinkedIn();
  const connectDice = useConnectDice();
  const disconnectDice = useDisconnectDice();

  const [radioOverride, setRadioOverride] = useState<Portal | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const radio = radioOverride ?? activePortal;

  const portalTitle = activePortal.charAt(0).toUpperCase() + activePortal.slice(1);
  const radioTitle = radio.charAt(0).toUpperCase() + radio.slice(1);

  const doSwitch = () =>
    switchPortal.mutate(radio, { onSuccess: () => setRadioOverride(null) });

  return (
    <aside
      aria-label="Portal, resume and profile controls"
      className="sticky top-0 flex h-dvh w-72 shrink-0 flex-col gap-3 overflow-y-auto border-r border-[#E3D9CB] bg-parchment-sidebar px-4 py-4"
    >
      {/* Brand */}
      <div className="flex items-center gap-3 border-b border-[#E3D9CB] pb-4">
        <Logo size={44} />
        <div>
          <div className="text-[1.02rem] font-extrabold leading-tight text-[#2D2416]">
            Rising Path
            <br />
            Recruiters
          </div>
          <div className="mt-0.5 text-[.64rem] font-extrabold uppercase tracking-[.1em] text-gold-text">
            Job Application Platform
          </div>
        </div>
      </div>

      {/* Portals — LIVE */}
      <SectionLabel>Portals</SectionLabel>
      <div className="flex flex-col gap-2" role="radiogroup" aria-label="Choose Active Portal">
        <PortalCard portal="linkedin" active={radio === "linkedin"} onSelect={() => setRadioOverride("linkedin")} />
        <PortalCard portal="dice" active={radio === "dice"} onSelect={() => setRadioOverride("dice")} />
      </div>

      {radio !== activePortal && (
        <>
          <Alert variant="warn">
            You are currently using {portalTitle}. Switching to {radioTitle} will disconnect the other portal
            session.
          </Alert>
          <Button variant="primary" size="full" disabled={switchPortal.isPending} onClick={doSwitch}>
            {switchPortal.isPending && <Loader2 className="size-4 animate-spin" />}
            Switch to {radioTitle}
          </Button>
          {switchPortal.isError && (
            <Alert variant="danger">{apiErrorMessage("Portal switch", switchPortal.error)}</Alert>
          )}
        </>
      )}

      <p className="text-[.78rem] text-[#6B7280]">Current active portal: {portalTitle}</p>

      {/* Connection status + connect/disconnect for the active portal — LIVE */}
      {activePortal === "linkedin" ? (
        <>
          <Alert variant={linkedinConnected ? "success" : "info"}>
            {linkedinConnected ? "LinkedIn connected" : "LinkedIn not connected"}
          </Alert>
          {linkedinConnected && (
            <Button size="full" disabled={disconnectLinkedIn.isPending} onClick={() => disconnectLinkedIn.mutate()}>
              {disconnectLinkedIn.isPending && <Loader2 className="size-4 animate-spin" />}
              Disconnect LinkedIn
            </Button>
          )}
          <Button
            size="full"
            disabled={linkedinConnected || connectLinkedIn.isPending}
            onClick={() => connectLinkedIn.mutate()}
          >
            {connectLinkedIn.isPending && <Loader2 className="size-4 animate-spin" />}
            {linkedinConnected ? "LinkedIn connected" : "Connect LinkedIn"}
          </Button>
          {connectLinkedIn.isPending && (
            <Alert variant="info">Opening LinkedIn login and saving session…</Alert>
          )}
          {connectLinkedIn.isError && (
            <Alert variant="danger">{apiErrorMessage("LinkedIn connection", connectLinkedIn.error)}</Alert>
          )}
          {disconnectLinkedIn.isError && (
            <Alert variant="danger">{apiErrorMessage("LinkedIn disconnect", disconnectLinkedIn.error)}</Alert>
          )}
        </>
      ) : (
        <>
          <Alert variant={diceConnected ? "success" : "info"}>
            {diceConnected ? "Dice connected" : "Dice not connected"}
          </Alert>
          {diceConnected && (
            <Button size="full" disabled={disconnectDice.isPending} onClick={() => disconnectDice.mutate()}>
              {disconnectDice.isPending && <Loader2 className="size-4 animate-spin" />}
              Disconnect Dice
            </Button>
          )}
          <Button
            size="full"
            disabled={diceConnected || connectDice.isPending}
            onClick={() => connectDice.mutate()}
          >
            {connectDice.isPending && <Loader2 className="size-4 animate-spin" />}
            {diceConnected ? "Dice connected" : "Connect Dice"}
          </Button>
          {connectDice.isPending && (
            <Alert variant="info">Opening Dice login. Complete login in the browser window…</Alert>
          )}
          {connectDice.isError && (
            <Alert variant="danger">{apiErrorMessage("Dice connection", connectDice.error)}</Alert>
          )}
          {disconnectDice.isError && (
            <Alert variant="danger">{apiErrorMessage("Dice disconnect", disconnectDice.error)}</Alert>
          )}
        </>
      )}

      {/* Resume — LIVE: POST /resumes (upload + analyze) */}
      <SectionLabel>Resume</SectionLabel>
      <label
        htmlFor="resume-upload"
        className="flex cursor-pointer flex-col items-center gap-1.5 rounded-xl border-[1.5px] border-dashed border-gold bg-[#FFFDF8] px-3 py-3.5 text-center transition-colors hover:border-gold-deep hover:bg-parchment-amber"
      >
        <UploadCloud className="size-5 text-gold-text" />
        <span className="text-sm font-semibold text-[#4B5563]">Upload Resume</span>
        <span className="text-xs text-[#6B7280]">
          {file?.name ?? "Drag & drop or browse a PDF, DOCX or TXT file."}
        </span>
        <input
          id="resume-upload"
          type="file"
          accept=".pdf,.docx,.txt"
          className="hidden"
          onChange={(e) => setFile(e.target.files?.[0] ?? null)}
        />
      </label>
      <Button
        variant="primary"
        size="full"
        disabled={file === null || upload.isPending}
        onClick={() => file && upload.mutate(file)}
      >
        {upload.isPending ? (
          <>
            <Loader2 className="size-4 animate-spin" /> Analyzing resume…
          </>
        ) : (
          "Analyze resume"
        )}
      </Button>
      {upload.isError && <Alert variant="danger">{apiErrorMessage("Resume analysis", upload.error)}</Alert>}
      {upload.isSuccess && <Alert variant="success">Resume analyzed</Alert>}

      {/* Profile — LIVE: GET /candidate-profile */}
      <SectionLabel>Profile</SectionLabel>
      {profile.isLoading ? (
        <Alert variant="info">Loading profile…</Alert>
      ) : profile.isError ? (
        <Alert variant="danger">{apiErrorMessage("Load profile", profile.error)}</Alert>
      ) : profile.data?.is_complete ? (
        <Alert variant="success">Profile complete</Alert>
      ) : profile.data ? (
        <Alert variant="info">Profile partially auto-filled</Alert>
      ) : (
        <Alert variant="info">Profile not loaded</Alert>
      )}
    </aside>
  );
}
