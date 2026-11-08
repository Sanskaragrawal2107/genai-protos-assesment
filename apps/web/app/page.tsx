"use client";

import Link from "next/link";
import { useCallback, useMemo, useState } from "react";
import {
  ArrowRight,
  CalendarClock,
  CheckCircle2,
  Loader2,
  Mail,
  RefreshCw,
  Sparkles,
  Star,
  Users,
  Video,
} from "lucide-react";
import { useWorkspace } from "@/app/providers";
import { api, ApiError } from "@/lib/api";
import { formatDateTime, providerLabel, timeFromNow } from "@/lib/format";
import type { DashboardData, PendingAction } from "@/lib/types";
import { useFetch } from "@/lib/useFetch";
import { PendingActions } from "@/components/PendingActions";
import {
  Alert,
  Card,
  CardHeader,
  EmptyState,
  PageHeader,
  SectionTitle,
  Spinner,
  StatusBadge,
} from "@/components/ui";

export default function DashboardPage() {
  const { me, refresh } = useWorkspace();
  const dashboard = useFetch<DashboardData>("/api/dashboard");
  const actions = useFetch<PendingAction[]>("/api/actions?status=pending");
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<{ tone: "success" | "error"; text: string } | null>(null);

  const reloadAll = useCallback(async () => {
    await Promise.all([dashboard.reload(), actions.reload(), refresh()]);
  }, [dashboard, actions, refresh]);

  async function runAction() {
    setBusy("sync");
    setNotice(null);
    try {
      const result = await api<{ status?: string; stats?: Record<string, number> }>("/api/sync/now", {
        method: "POST",
      });
      setNotice({
        tone: "success",
        text: `Inbox sync finished (${result.status || "completed"}) — ${JSON.stringify(result.stats || {})}`,
      });
      await reloadAll();
    } catch (err) {
      setNotice({
        tone: "error",
        text: err instanceof ApiError ? `${err.message}${err.hint ? ` — ${err.hint}` : ""}` : String(err),
      });
    } finally {
      setBusy(null);
    }
  }

  const totals = dashboard.data?.totals;
  const byStatus = dashboard.data?.by_status || {};
  const pipelineTotal = useMemo(
    () => Object.values(byStatus).reduce((sum, value) => sum + value, 0),
    [byStatus],
  );

  const gmail = me?.connectors.find((connector) => connector.provider === "gmail");
  const gmailActive = gmail?.status === "active";

  return (
    <div className="space-y-6">
      <PageHeader
        title="Overview"
        subtitle={
          gmailActive
            ? `Applications are pulled from ${gmail?.connected_email || "your connected inbox"} and screened against every open role.`
            : "Connect a Gmail account to start ingesting applications automatically."
        }
        meta={
          <>
            <span className="badge badge-slate">
              <span
                className={`h-1 w-1 rounded-full ${gmailActive ? "bg-[#2c7a4b]" : "bg-[#a1a1aa]"}`}
              />
              {gmailActive ? "Inbox connected" : "Inbox not connected"}
            </span>
            <span className="meta">
              {dashboard.data?.recent_ingestion?.[0]
                ? `Last sync ${timeFromNow(dashboard.data.recent_ingestion[0].created_at)}`
                : "No sync has run yet"}
            </span>
          </>
        }
        actions={
          <>
            <button className="btn-secondary" onClick={() => runAction()} disabled={busy !== null}>
              {busy === "sync" ? (
                <Loader2 size={14} className="animate-spin" />
              ) : (
                <RefreshCw size={14} />
              )}
              Sync inbox
            </button>
            <Link href="/assistant" className="btn-primary">
              <Sparkles size={14} /> Ask the assistant
            </Link>
          </>
        }
      />

      {notice ? (
        <Alert tone={notice.tone === "success" ? "success" : "error"}>{notice.text}</Alert>
      ) : null}

      {dashboard.error ? (
        <Alert tone="error" title="Could not load the overview">
          {dashboard.error.message}
          {dashboard.error.hint ? ` — ${dashboard.error.hint}` : ""}
        </Alert>
      ) : null}

      <Card className="overflow-hidden">
        <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6">
          <Metric
            label="Candidates"
            value={dashboard.loading ? null : totals?.candidates ?? 0}
          />
          <Metric
            label="New this week"
            value={dashboard.loading ? null : totals?.new_this_week ?? 0}
            hint="From Gmail"
          />
          <Metric
            label="Awaiting screening"
            value={dashboard.loading ? null : totals?.awaiting_screening ?? 0}
          />
          <Metric
            label="Shortlisted"
            value={dashboard.loading ? null : totals?.shortlisted ?? 0}
          />
          <Metric
            label="Interviews"
            value={dashboard.loading ? null : totals?.interviews_scheduled ?? 0}
          />
          <Metric
            label="Approvals"
            value={dashboard.loading ? null : totals?.pending_followups ?? 0}
            hint="Waiting on you"
            accent={Boolean(totals?.pending_followups)}
          />
        </div>
      </Card>

      <div className="grid gap-4 lg:grid-cols-3">
        <Card className="lg:col-span-2">
          <CardHeader
            title="Hiring pipeline"
            subtitle="Candidates by stage, updated as resumes arrive"
            action={
              <Link href="/candidates" className="btn-ghost btn-sm">
                View candidates <ArrowRight size={13} />
              </Link>
            }
          />
          <div className="space-y-3.5 px-5 py-4">
            {pipelineTotal === 0 ? (
              <div className="py-6">
                <p className="text-[13px] font-medium text-[#3f3f46]">No candidates yet</p>
                <p className="mt-1 max-w-md text-[12px] leading-5 text-[#8a8a93]">
                  Once applications reach your connected inbox, candidates are screened and listed
                  here.
                </p>
              </div>
            ) : (
              Object.entries(byStatus)
                .sort((a, b) => b[1] - a[1])
                .map(([status, count]) => {
                  const width = Math.max(2, Math.round((count / pipelineTotal) * 100));
                  return (
                    <div key={status} className="flex items-center gap-3">
                      <span className="w-[132px] shrink-0 truncate text-[12px] capitalize text-[#52525b]">
                        {status.replaceAll("_", " ").toLowerCase()}
                      </span>
                      <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-[#f2f2f0]">
                        <div
                          className="h-full rounded-full bg-[#1f4b3f]"
                          style={{ width: `${width}%` }}
                        />
                      </div>
                      <span className="w-7 shrink-0 text-right text-[12px] font-medium tabular-nums text-[#27272a]">
                        {count}
                      </span>
                    </div>
                  );
                })
            )}
          </div>
          {pipelineTotal > 0 ? (
            <div className="flex items-center justify-between border-t border-[#f0f0ed] px-5 py-2.5">
              <span className="meta">
                {pipelineTotal} candidate{pipelineTotal === 1 ? "" : "s"} across{" "}
                {Object.keys(byStatus).length} stage{Object.keys(byStatus).length === 1 ? "" : "s"}
              </span>
              <Link href="/shortlists" className="link text-[11.5px]">
                Open shortlists
              </Link>
            </div>
          ) : null}
        </Card>

        <Card className="flex flex-col">
          <CardHeader
            title="Upcoming interviews"
            subtitle="Confirmed calendar events"
            action={
              <Link href="/interviews" className="btn-ghost btn-sm">
                <CalendarClock size={13} /> Calendar
              </Link>
            }
          />
          {dashboard.loading ? (
            <div className="p-5">
              <Spinner />
            </div>
          ) : (dashboard.data?.upcoming_interviews.length ?? 0) === 0 ? (
            <EmptyState
              icon={<Video size={20} />}
              title="No interviews booked"
              description="Schedule from a shortlisted candidate to create a Teams or Google Meet room and email the invitation."
            />
          ) : (
            <ul className="divide-y divide-[#f4f4f1]">
              {dashboard.data?.upcoming_interviews.map((item) => (
                <li key={item.id} className="px-5 py-3">
                  <div className="flex items-center justify-between gap-2">
                    <p className="truncate text-[12.5px] font-medium text-[#18181b]">
                      {item.candidate_name}
                    </p>
                    <StatusBadge status={item.status} />
                  </div>
                  <p className="mt-0.5 text-[11px] text-[#8a8a93]">
                    {formatDateTime(item.start_time)} · {providerLabel(item.provider)}
                  </p>
                  {item.meeting_url ? (
                    <a
                      href={item.meeting_url}
                      target="_blank"
                      rel="noreferrer"
                      className="link mt-1 inline-flex items-center gap-1 text-[11px]"
                    >
                      Join link <ArrowRight size={10} />
                    </a>
                  ) : null}
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader
            title="Waiting for your approval"
            subtitle="Nothing is emailed or booked without you"
            action={
              <Link href="/communications" className="btn-ghost btn-sm">
                All actions
              </Link>
            }
          />
          <PendingActions actions={actions.data} loading={actions.loading} onChanged={reloadAll} />
        </Card>

        <Card>
          <CardHeader title="Inbox syncs" subtitle="Newest ingestion runs first" />
          {dashboard.loading ? (
            <div className="p-5">
              <Spinner />
            </div>
          ) : (dashboard.data?.recent_ingestion.length ?? 0) === 0 ? (
            <EmptyState
              icon={<Mail size={20} />}
              title="No sync runs yet"
              description="Press “Sync inbox” to pull applications from Gmail."
            />
          ) : (
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>Started</th>
                    <th>Trigger</th>
                    <th>Status</th>
                    <th className="text-right">Found</th>
                    <th className="text-right">Imported</th>
                  </tr>
                </thead>
                <tbody>
                  {dashboard.data?.recent_ingestion.map((job) => (
                    <tr key={job.id}>
                      <td className="whitespace-nowrap text-[11.5px] text-[#71717a]">
                        {timeFromNow(job.created_at)}
                      </td>
                      <td className="text-[11.5px] capitalize text-[#52525b]">{job.trigger}</td>
                      <td>
                        <StatusBadge status={job.status} />
                      </td>
                      <td className="text-right text-[11.5px] tabular-nums text-[#52525b]">
                        {job.stats?.messages ?? job.stats?.found ?? 0}
                      </td>
                      <td className="text-right text-[11.5px] tabular-nums text-[#52525b]">
                        {job.stats?.imported ?? 0}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      </div>

      <div>
        <SectionTitle hint="Last 10 events">Activity</SectionTitle>
        <Card>
          {dashboard.loading ? (
            <div className="p-5">
              <Spinner />
            </div>
          ) : (dashboard.data?.recent_activity.length ?? 0) === 0 ? (
            <EmptyState icon={<CheckCircle2 size={20} />} title="No activity recorded yet" />
          ) : (
            <ul className="divide-y divide-[#f4f4f1]">
              {dashboard.data?.recent_activity.map((event) => (
                <li key={event.id} className="flex items-center gap-3 px-5 py-2.5">
                  <span className="h-1 w-1 shrink-0 rounded-full bg-[#b9c9c2]" />
                  <p className="flex-1 truncate text-[11.5px] text-[#52525b]">
                    <span className="font-medium text-[#27272a]">{event.action}</span>
                    {event.entity_type ? (
                      <span className="text-[#a1a1aa]"> · {event.entity_type}</span>
                    ) : null}
                  </p>
                  <span className="shrink-0 text-[10.5px] text-[#a1a1aa]">
                    {timeFromNow(event.created_at)}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>

      <div className="grid gap-3 sm:grid-cols-3">
        <QuickLink
          href="/candidates?status=SHORTLISTED"
          icon={<Star size={15} />}
          title="Review shortlist"
          hint="Contact or schedule interviews"
        />
        <QuickLink
          href="/jobs"
          icon={<Users size={15} />}
          title="Screen a role"
          hint="Rank candidates by fit"
        />
        <QuickLink
          href="/settings/sync"
          icon={<RefreshCw size={15} />}
          title="Tune inbox sync"
          hint="From 15 minutes to once a day"
        />
      </div>
    </div>
  );
}

function Metric({
  label,
  value,
  hint,
  accent = false,
}: {
  label: string;
  value: number | null;
  hint?: string;
  accent?: boolean;
}) {
  return (
    <div className="border-b border-[#f0f0ed] px-4 py-3.5 sm:border-b-0 sm:border-r sm:last:border-r-0">
      <p className="stat-label">{label}</p>
      <p
        className={`mt-1.5 text-[24px] font-semibold leading-8 tracking-[-0.03em] tabular-nums ${
          accent ? "text-[#b4543f]" : "text-[#18181b]"
        }`}
      >
        {value === null ? <span className="text-[#d4d4d8]">—</span> : value}
      </p>
      {hint ? <p className="mt-0.5 truncate text-[11px] text-[#a1a1aa]">{hint}</p> : null}
    </div>
  );
}

function QuickLink({
  href,
  icon,
  title,
  hint,
}: {
  href: string;
  icon: React.ReactNode;
  title: string;
  hint: string;
}) {
  return (
    <Link
      href={href}
      className="group flex items-center gap-3 rounded-[10px] border border-[#e7e7e4] bg-white p-3.5 transition-colors hover:border-[#d8d8d4] hover:bg-[#fcfcfb]"
    >
      <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-[7px] border border-[#ececea] bg-[#fafaf9] text-[#71717a] transition-colors group-hover:border-[#dfe7e3] group-hover:bg-[#eef3f0] group-hover:text-[#1f4b3f]">
        {icon}
      </span>
      <span className="min-w-0 flex-1">
        <span className="block truncate text-[12.5px] font-medium text-[#18181b]">{title}</span>
        <span className="block truncate text-[11px] text-[#8a8a93]">{hint}</span>
      </span>
      <ArrowRight
        size={13}
        className="shrink-0 text-[#d4d4d8] transition-colors group-hover:text-[#1f4b3f]"
      />
    </Link>
  );
}
