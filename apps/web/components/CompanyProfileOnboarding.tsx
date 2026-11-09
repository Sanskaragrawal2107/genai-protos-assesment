"use client";

import { useEffect, useState } from "react";
import { useWorkspace } from "@/app/providers";
import { CompanyProfileForm } from "@/components/CompanyProfileForm";
import { Modal } from "@/components/ui";

const DISMISS_PREFIX = "recruiter.companyProfile.dismissed.";

/**
 * First-run prompt. Shows once per workspace (until dismissed or completed) so
 * the recruiter can save the company name, phone and interviewer that every
 * email will then use automatically.
 */
export function CompanyProfileOnboarding() {
  const { me, loading } = useWorkspace();
  // Start closed so the modal never flashes during hydration; the effect below
  // opens it only when the profile is genuinely missing.
  const [dismissed, setDismissed] = useState(true);
  const workspaceId = me?.workspace.id ?? "";

  useEffect(() => {
    if (!workspaceId) return;
    setDismissed(window.localStorage.getItem(`${DISMISS_PREFIX}${workspaceId}`) === "1");
  }, [workspaceId]);

  const open = Boolean(me && !loading && !me.workspace.profile_complete && !dismissed);

  function dismiss() {
    if (workspaceId) {
      window.localStorage.setItem(`${DISMISS_PREFIX}${workspaceId}`, "1");
    }
    setDismissed(true);
  }

  if (!open) return null;

  return (
    <Modal
      open={open}
      onClose={dismiss}
      title="Set up your company profile"
      width="max-w-lg"
    >
      <p className="mb-4 text-[12px] leading-5 text-[#71717a]">
        Add these once and the assistant will stop asking for them. Every email and interview
        invitation is signed with them automatically.
      </p>
      <CompanyProfileForm submitLabel="Save and continue" onSaved={dismiss} onSkip={dismiss} />
    </Modal>
  );
}