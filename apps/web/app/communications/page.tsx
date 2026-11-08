"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import { Mail, MailCheck, Search } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import type { Communication, PendingAction } from "@/lib/types";
import { useFetch } from "@/lib/useFetch";
import { PendingActions } from "@/components/PendingActions";
import { Alert, Avatar, Card, EmptyState, PageHeader, Spinner, StatusBadge } from "@/components/ui";

export default function CommunicationsPage() {
  const comms = useFetch<Communication[]>("/api/communications?limit=200");
  const actions = useFetch<PendingAction[]>("/api/actions?status=pending");
  const [query, setQuery] = useState("");
  const [sending, setSending] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return (comms.data || []).filter((comm) =>
      needle
        ? [comm.candidate_name, comm.subject, comm.kind, comm.content]
            .filter(Boolean)
            .join(" ")
            .toLowerCase()
            .includes(needle)
        : true,
    );
  }, [comms.data, query]);

  const pendingEmails = (actions.data || []).filter((action) => action.kind === "send_email");

  async function sendNow(id: string) {
    setSending(id);
    setError(null);
    try {
      await api(`/api/communications/${id}/send`, { method: "POST" });
      await comms.reload();
    } catch (err) {
      setError(err instanceof ApiError ? `${err.message}${err.hint ? ` — ${err.hint}` : ""}` : String(err));
    } finally {
      setSending(null);
    }
  }

  return (
    <div className="space-y-5">
      <PageHeader
        title="Inbox & outreach"
        subtitle="Messages are sent from your connected Gmail account. Drafts wait for your approval."
      />

      {error ? <Alert tone="error">{error}</Alert> : null}
      {comms.error ? (
        <Alert tone="error" title="Could not load communications">
          {comms.error.message}
        </Alert>
      ) : null}

      {pendingEmails.length > 0 ? (
        <Card>
          <div className="card-hd">
            <div>
              <h2 className="card-title">Drafts waiting for approval</h2>
              <p className="card-sub mt-0.5">Approve to send through Gmail.</p>
            </div>
            <span className="badge-amber">{pendingEmails.length} pending</span>
          </div>
          <PendingActions
            actions={pendingEmails}
            loading={actions.loading}
            onChanged={() => {
              void actions.reload();
              void comms.reload();
            }}
          />
        </Card>
      ) : null}

      <Card>
        <div className="card-hd">
          <div>
            <h2 className="card-title">History</h2>
            <p className="card-sub mt-0.5">{filtered.length} messages</p>
          </div>
          <div className="relative w-full max-w-xs">
            <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-[#a1a1aa]" />
            <input
              className="input pl-8"
              placeholder="Filter by candidate or subject"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
            />
          </div>
        </div>

        {comms.loading ? (
          <div className="p-6">
            <Spinner label="Loading messages…" />
          </div>
        ) : filtered.length === 0 ? (
          <EmptyState
            icon={<Mail size={26} />}
            title="No messages yet"
            description="Draft a follow-up from a candidate profile or ask the assistant to draft one. Approved drafts are sent from Gmail."
          />
        ) : (
          <ul className="divide-y divide-[#f4f4f1]">
            {filtered.map((comm) => (
              <li key={comm.id} className="flex flex-col gap-3 px-5 py-3.5 sm:flex-row sm:items-start">
                <Avatar name={comm.candidate_name} size={34} />
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <Link
                      href={`/candidates/${comm.candidate_id}`}
                      className="text-[12.5px] font-medium text-[#18181b] hover:text-[#1f4b3f]"
                    >
                      {comm.candidate_name || "Candidate"}
                    </Link>
                    <span className="chip">{comm.kind.replaceAll("_", " ")}</span>
                    <StatusBadge status={comm.status} />
                  </div>
                  <p className="mt-1 truncate text-[12px] font-medium text-[#3f3f46]">
                    {comm.subject || "(no subject)"}
                  </p>
                  <p className="mt-1 line-clamp-2 whitespace-pre-wrap text-[11.5px] leading-5 text-[#8a8a93]">
                    {comm.content || ""}
                  </p>
                  {comm.error_message ? (
                    <p className="mt-1 text-[11px] text-[#a33a2c]">{comm.error_message}</p>
                  ) : null}
                  <p className="mt-1 text-[10.5px] text-[#a1a1aa]">
                    Created {formatDateTime(comm.created_at)}
                    {comm.sent_at ? ` · sent ${formatDateTime(comm.sent_at)}` : ""}
                  </p>
                </div>
                <div className="flex shrink-0 items-center gap-2">
                  {comm.status === "queued" || comm.status === "draft" || comm.status === "failed" ? (
                    <button
                      className="btn-secondary btn-sm"
                      disabled={sending !== null}
                      onClick={() => void sendNow(comm.id)}
                    >
                      {sending === comm.id ? (
                        <span className="h-3 w-3 animate-spin rounded-full border-2 border-[#e4e4e7] border-t-[#1f4b3f]" />
                      ) : (
                        <MailCheck size={13} />
                      )}
                      Send via Gmail
                    </button>
                  ) : null}
                </div>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}
