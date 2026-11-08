"use client";

import { useState } from "react";
import { CalendarPlus, Check, Mail, ShieldCheck, X } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { formatDateTime, providerLabel } from "@/lib/format";
import type { PendingAction } from "@/lib/types";
import { EmptyState, Spinner } from "./ui";

interface Props {
  actions: PendingAction[] | null;
  loading?: boolean;
  onChanged?: () => void;
  emptyHint?: string;
}

function summarize(action: PendingAction) {
  const payload = action.payload || {};
  if (action.kind === "schedule_interview") {
    return [
      payload.candidate_name ? `Candidate: ${payload.candidate_name}` : null,
      payload.start ? `Start: ${formatDateTime(String(payload.start))}` : null,
      payload.provider ? `Video: ${providerLabel(String(payload.provider))}` : null,
      payload.timezone ? `Timezone: ${payload.timezone}` : null,
    ].filter(Boolean) as string[];
  }
  if (action.kind === "send_email") {
    return [
      payload.subject ? `Subject: ${payload.subject}` : null,
      payload.candidate_id ? "Sends through the connected Gmail account" : null,
    ].filter(Boolean) as string[];
  }
  return Object.entries(payload)
    .slice(0, 4)
    .map(([key, value]) => `${key.replaceAll("_", " ")}: ${String(value)}`);
}

export function PendingActions({ actions, loading, onChanged, emptyHint }: Props) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [completedIds, setCompletedIds] = useState<Record<string, "approved" | "rejected">>({});

  async function run(id: string, verb: "approve" | "reject") {
    setBusy(`${verb}:${id}`);
    setError(null);
    try {
      await api(`/api/actions/${id}/${verb}`, { method: "POST" });
      const status: "approved" | "rejected" = verb === "approve" ? "approved" : "rejected";
      setCompletedIds((prev) => ({ ...prev, [id]: status }));
      setTimeout(() => {
        onChanged?.();
      }, 600);
    } catch (err) {
      setError(err instanceof ApiError ? `${err.message}${err.hint ? ` — ${err.hint}` : ""}` : String(err));
    } finally {
      setBusy(null);
    }
  }

  if (loading) {
    return (
      <div className="px-5 py-6">
        <Spinner label="Loading approvals…" />
      </div>
    );
  }

  if (!actions || actions.length === 0) {
    return (
      <EmptyState
        icon={<ShieldCheck size={20} />}
        title="Nothing waiting for approval"
        description={emptyHint || "Drafted emails and interview proposals appear here before anything is sent or booked."}
      />
    );
  }

  return (
    <div>
      {error ? (
        <div className="mx-5 mt-4 rounded-[8px] border border-[#f0d7d3] bg-[#fdf6f4] px-3 py-2 text-[11.5px] text-[#9a3a2c]">
          {error}
        </div>
      ) : null}
      <ul className="divide-y divide-[#f4f4f1]">
        {actions.map((action) => {
          const Icon = action.kind === "schedule_interview" ? CalendarPlus : Mail;
          const details = summarize(action);
          return (
            <li key={action.id} className="flex flex-col gap-3 px-5 py-3.5 sm:flex-row sm:items-center">
              <div className="flex min-w-0 flex-1 items-start gap-3">
                <span className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-[7px] border border-[#ececea] bg-[#fafaf9] text-[#71717a]">
                  <Icon size={16} />
                </span>
                <div className="min-w-0">
                  <p className="truncate text-[12.5px] font-medium text-[#18181b]">{action.title}</p>
                  <p className="mt-0.5 text-[11px] leading-4 text-[#8a8a93]">
                    {details.join(" · ") || action.kind.replaceAll("_", " ")}
                  </p>
                </div>
              </div>
              <div className="flex shrink-0 items-center gap-2">
                {completedIds[action.id] === "approved" ? (
                  <div className="flex items-center gap-1.5 rounded-md bg-emerald-50 px-3 py-1.5 text-[12px] font-medium text-emerald-700 border border-emerald-200">
                    <Check size={14} className="text-emerald-600" />
                    <span>Sent via Gmail</span>
                  </div>
                ) : completedIds[action.id] === "rejected" ? (
                  <div className="flex items-center gap-1.5 rounded-md bg-stone-100 px-3 py-1.5 text-[12px] font-medium text-stone-600 border border-stone-200">
                    <X size={14} className="text-stone-500" />
                    <span>Rejected</span>
                  </div>
                ) : (
                  <>
                    <button
                      className="btn-secondary btn-sm"
                      disabled={busy !== null}
                      onClick={() => run(action.id, "reject")}
                    >
                      <X size={13} /> Reject
                    </button>
                    <button
                      className="btn-primary btn-sm"
                      disabled={busy !== null}
                      onClick={() => run(action.id, "approve")}
                    >
                      {busy === `approve:${action.id}` ? (
                        <span className="h-3 w-3 animate-spin rounded-full border-2 border-white/50 border-t-white" />
                      ) : (
                        <Check size={13} />
                      )}
                      Approve & Send
                    </button>
                  </>
                )}
              </div>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
