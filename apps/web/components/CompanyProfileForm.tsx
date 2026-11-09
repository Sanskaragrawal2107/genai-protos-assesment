"use client";

import { useEffect, useState } from "react";
import { Building2, Loader2, Phone, UserRound } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { useWorkspace } from "@/app/providers";
import { Alert } from "@/components/ui";

interface Props {
  onSaved?: () => void;
  onSkip?: () => void;
  submitLabel?: string;
}

/**
 * Company details saved once and reused automatically in every email.
 *
 * The backend stores these in ``workspaces.settings`` and the email hydrator
 * fills ``[Company]`` / ``[Interviewer Name]`` / ``[Phone]`` from them, so the
 * assistant never has to ask for them again.
 */
export function CompanyProfileForm({ onSaved, onSkip, submitLabel = "Save company profile" }: Props) {
  const { me, refresh } = useWorkspace();
  const [companyName, setCompanyName] = useState("");
  const [companyPhone, setCompanyPhone] = useState("");
  const [interviewerName, setInterviewerName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  // Keep the form in sync when the workspace loads or refreshes.
  useEffect(() => {
    setCompanyName(me?.workspace.company_name ?? "");
    setCompanyPhone(me?.workspace.company_phone ?? "");
    setInterviewerName(me?.workspace.interviewer_name ?? "");
  }, [me?.workspace.company_name, me?.workspace.company_phone, me?.workspace.interviewer_name]);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    setSaved(false);
    try {
      await api("/api/workspace/profile", {
        method: "PUT",
        body: {
          company_name: companyName.trim(),
          company_phone: companyPhone.trim(),
          interviewer_name: interviewerName.trim(),
        },
      });
      await refresh();
      setSaved(true);
      onSaved?.();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="space-y-4" onSubmit={submit}>
      <div>
        <label className="label" htmlFor="company-name">
          Company name
        </label>
        <div className="relative">
          <Building2
            size={14}
            className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-[#a1a1aa]"
          />
          <input
            id="company-name"
            className="input pl-9"
            placeholder="Acme Technologies"
            value={companyName}
            onChange={(event) => setCompanyName(event.target.value)}
            maxLength={200}
          />
        </div>
        <p className="mt-1 text-[11px] text-[#a1a1aa]">
          Used in every email signature and interview invitation.
        </p>
      </div>

      <div>
        <label className="label" htmlFor="company-phone">
          Company phone number <span className="text-[#a1a1aa]">(optional)</span>
        </label>
        <div className="relative">
          <Phone
            size={14}
            className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-[#a1a1aa]"
          />
          <input
            id="company-phone"
            className="input pl-9"
            placeholder="+1 415 555 0134"
            value={companyPhone}
            onChange={(event) => setCompanyPhone(event.target.value)}
            maxLength={60}
          />
        </div>
        <p className="mt-1 text-[11px] text-[#a1a1aa]">
          The assistant writes a [Phone] placeholder and the server fills it from here. If it is
          empty, the phone line is omitted rather than inventing a number.
        </p>
      </div>

      <div>
        <label className="label" htmlFor="interviewer-name">
          Interviewer / host name
        </label>
        <div className="relative">
          <UserRound
            size={14}
            className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-[#a1a1aa]"
          />
          <input
            id="interviewer-name"
            className="input pl-9"
            placeholder="Priya Sharma"
            value={interviewerName}
            onChange={(event) => setInterviewerName(event.target.value)}
            maxLength={160}
          />
        </div>
        <p className="mt-1 text-[11px] text-[#a1a1aa]">
          The person who will take the interview; signs the invitation email.
        </p>
      </div>

      {error ? <Alert tone="error">{error}</Alert> : null}
      {saved ? <Alert tone="success">Company profile saved.</Alert> : null}

      <div className="flex items-center gap-2">
        <button className="btn-primary" type="submit" disabled={busy}>
          {busy ? <Loader2 size={14} className="animate-spin" /> : null}
          {submitLabel}
        </button>
        {onSkip ? (
          <button type="button" className="btn-secondary" onClick={onSkip} disabled={busy}>
            Do this later
          </button>
        ) : null}
      </div>
    </form>
  );
}