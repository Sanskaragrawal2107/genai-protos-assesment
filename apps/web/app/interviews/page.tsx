"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import { CalendarDays, ExternalLink, Loader2, RotateCcw, X } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { formatDateTime, providerLabel, timeFromNow } from "@/lib/format";
import type { Interview, PendingAction } from "@/lib/types";
import { useFetch } from "@/lib/useFetch";
import { PendingActions } from "@/components/PendingActions";
import { Alert, Avatar, Card, EmptyState, PageHeader, Spinner, StatusBadge } from "@/components/ui";

export default function InterviewsPage() {
  const interviews = useFetch<Interview[]>("/api/interviews?limit=200");
  const actions = useFetch<PendingAction[]>("/api/actions?status=pending");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const now = Date.now();
  const rows = interviews.data || [];
  const upcoming = useMemo(
    () =>
      rows
        .filter((row) => row.status !== "cancelled" && row.start_time && new Date(row.start_time).getTime() >= now - 3600_000)
        .sort((a, b) => new Date(a.start_time || 0).getTime() - new Date(b.start_time || 0).getTime()),
    [rows, now],
  );
  const past = useMemo(
    () =>
      rows
        .filter((row) => !upcoming.includes(row))
        .sort((a, b) => new Date(b.start_time || 0).getTime() - new Date(a.start_time || 0).getTime()),
    [rows, upcoming],
  );

  async function cancel(id: string) {
    if (!window.confirm("Cancel this interview and delete the Microsoft Teams meeting?")) return;
    setBusy(`cancel:${id}`);
    setError(null);
    try {
      await api(`/api/interviews/${id}/cancel`, { method: "POST" });
      await interviews.reload();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  }

  async function reschedule(id: string) {
    const raw = window.prompt("New start (ISO or YYYY-MM-DDTHH:MM, local time):");
    if (!raw) return;
    const parsed = new Date(raw);
    if (Number.isNaN(parsed.getTime())) {
      setError("That date could not be parsed.");
      return;
    }
    setBusy(`reschedule:${id}`);
    setError(null);
    try {
      await api(`/api/interviews/${id}/reschedule`, {
        method: "POST",
        body: { start: parsed.toISOString() },
      });
      await interviews.reload();
      await actions.reload();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  }

  const pendingSchedules = (actions.data || []).filter((action) => action.kind === "schedule_interview");

  return (
    <div className="space-y-5">
      <PageHeader
        title="Interviews"
        subtitle="Microsoft Teams meetings with Gmail invitations."
        actions={
          <Link href="/candidates" className="btn-secondary">
            <CalendarDays size={14} /> Schedule from a candidate
          </Link>
        }
      />

      {error ? <Alert tone="error">{error}</Alert> : null}
      {interviews.error ? (
        <Alert tone="error" title="Could not load interviews">
          {interviews.error.message}
        </Alert>
      ) : null}

      {pendingSchedules.length > 0 ? (
        <Card>
          <div className="card-hd">
            <div>
              <h2 className="card-title">Interviews waiting for approval</h2>
              <p className="card-sub mt-0.5">No Teams meeting exists until you approve.</p>
            </div>
            <span className="badge-amber">{pendingSchedules.length} pending</span>
          </div>
          <PendingActions
            actions={pendingSchedules}
            loading={actions.loading}
            onChanged={() => {
              void actions.reload();
              void interviews.reload();
            }}
          />
        </Card>
      ) : null}

      <Card>
        <div className="card-hd">
          <div>
            <h2 className="card-title">Upcoming</h2>
            <p className="card-sub mt-0.5">{upcoming.length} scheduled</p>
          </div>
        </div>
        {interviews.loading ? (
          <div className="p-6">
            <Spinner label="Loading interviews…" />
          </div>
        ) : upcoming.length === 0 ? (
          <EmptyState
            icon={<CalendarDays size={26} />}
            title="No upcoming interviews"
            description="Schedule one from a shortlisted candidate. The Teams link and Gmail invitation are created after approval."
            action={
              <Link href="/shortlists" className="btn-secondary btn-sm">
                Open shortlists
              </Link>
            }
          />
        ) : (
          <ul className="divide-y divide-[#f4f4f1]">
            {upcoming.map((interview) => (
              <li key={interview.id} className="flex flex-col gap-3 px-5 py-3.5 sm:flex-row sm:items-center">
                <Avatar name={interview.candidate_name || interview.candidate_id} size={34} />
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <Link
                      href={`/candidates/${interview.candidate_id}`}
                      className="text-[12.5px] font-medium text-[#18181b] hover:text-[#1f4b3f]"
                    >
                      {interview.candidate_name || "Candidate"}
                    </Link>
                    <StatusBadge status={interview.status} />
                    <span className="badge-slate">{providerLabel(interview.provider)}</span>
                  </div>
                  <p className="mt-1 text-[11.5px] text-[#52525b]">
                    {formatDateTime(interview.start_time)} · {timeFromNow(interview.start_time)} ·{" "}
                    {interview.timezone}
                  </p>
                  <p className="mt-0.5 text-[10.5px] text-[#a1a1aa]">
                    Invitation: {interview.invite_status.replaceAll("_", " ")}
                  </p>
                </div>
                <div className="flex shrink-0 flex-wrap items-center gap-2">
                  {interview.meeting_url ? (
                    <a
                      href={interview.meeting_url}
                      target="_blank"
                      rel="noreferrer"
                      className="btn-secondary btn-sm"
                    >
                      <ExternalLink size={13} /> Join
                    </a>
                  ) : (
                    <span className="badge-rose">No join link</span>
                  )}
                  <button
                    className="btn-secondary btn-sm"
                    onClick={() => void reschedule(interview.id)}
                    disabled={busy !== null}
                  >
                    {busy === `reschedule:${interview.id}` ? (
                      <Loader2 size={13} className="animate-spin" />
                    ) : (
                      <RotateCcw size={13} />
                    )}
                    Reschedule
                  </button>
                  <button
                    className="btn-danger btn-sm"
                    onClick={() => void cancel(interview.id)}
                    disabled={busy !== null}
                  >
                    {busy === `cancel:${interview.id}` ? (
                      <Loader2 size={13} className="animate-spin" />
                    ) : (
                      <X size={13} />
                    )}
                    Cancel
                  </button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </Card>

      {past.length > 0 ? (
        <Card>
          <div className="card-hd">
            <h2 className="card-title">Past & cancelled</h2>
          </div>
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Candidate</th>
                  <th>When</th>
                  <th>Provider</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {past.map((interview) => (
                  <tr key={interview.id}>
                    <td>
                      <Link href={`/candidates/${interview.candidate_id}`} className="text-[11.5px] font-medium text-[#27272a] hover:text-[#1f4b3f]">
                        {interview.candidate_name || interview.candidate_id.slice(0, 8)}
                      </Link>
                    </td>
                    <td className="whitespace-nowrap text-[11.5px] text-[#52525b]">{formatDateTime(interview.start_time)}</td>
                    <td className="text-[11.5px] text-[#52525b]">{providerLabel(interview.provider)}</td>
                    <td>
                      <StatusBadge status={interview.status} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      ) : null}
    </div>
  );
}
