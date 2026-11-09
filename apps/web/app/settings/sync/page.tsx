"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { Inbox, Loader2, Play, Save } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { formatDateTime, timeFromNow } from "@/lib/format";
import type { SyncJob, SyncSettingsResponse } from "@/lib/types";
import { useFetch } from "@/lib/useFetch";
import { Alert, Card, CardHeader, EmptyState, PageHeader, Spinner, StatusBadge } from "@/components/ui";

export default function SyncSettingsPage() {
  const settings = useFetch<SyncSettingsResponse>("/api/sync/settings");
  const jobs = useFetch<SyncJob[]>("/api/sync/jobs?limit=20");

  const [enabled, setEnabled] = useState(true);
  const [interval, setInterval] = useState(30);
  const [custom, setCustom] = useState("");
  const [query, setQuery] = useState("newer_than:2d");
  const [busy, setBusy] = useState<"save" | "run" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    const current = settings.data?.settings;
    if (!current) return;
    setEnabled(current.enabled);
    setInterval(current.interval_minutes);
    setCustom(
      settings.data?.presets?.includes(current.interval_minutes) ? "" : String(current.interval_minutes),
    );
    setQuery(current.query);
  }, [settings.data]);

  async function save() {
    setBusy("save");
    setError(null);
    setNotice(null);
    try {
      const minutes = custom.trim() ? Number(custom.trim()) : interval;
      await api("/api/sync/settings", {
        method: "PUT",
        body: { enabled, interval_minutes: minutes, query },
      });
      setNotice("Sync settings saved.");
      await settings.reload();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  }

  async function runNow() {
    setBusy("run");
    setError(null);
    setNotice(null);
    try {
      const result = await api<{ status?: string; job?: { id?: string } }>("/api/sync/now", {
        method: "POST",
      });
      setNotice(
        result.status === "already_running"
          ? "A sync is already running."
          : "Sync finished. New resumes appear under Candidates.",
      );
      await settings.reload();
      await jobs.reload();
    } catch (err) {
      setError(err instanceof ApiError ? `${err.message}${err.hint ? ` — ${err.hint}` : ""}` : String(err));
    } finally {
      setBusy(null);
    }
  }

  const connected = settings.data?.connected;
  const current = settings.data?.settings;

  return (
    <div className="space-y-5">
      <PageHeader
        title="Inbox sync"
        subtitle="Gmail is checked for resume attachments on the schedule below."
        actions={
          <>
            <Link href="/settings/integrations" className="btn-secondary btn-sm">
              Integrations
            </Link>
            <button className="btn-primary btn-sm" onClick={() => void runNow()} disabled={busy !== null || !connected}>
              {busy === "run" ? <Loader2 size={13} className="animate-spin" /> : <Play size={13} />}
              Sync now
            </button>
          </>
        }
      />

      {error ? <Alert tone="error">{error}</Alert> : null}
      {notice ? <Alert tone="success">{notice}</Alert> : null}

      {settings.error ? (
        <Alert tone="error" title="Could not load sync settings">
          {settings.error.message}
        </Alert>
      ) : null}

      {!settings.loading && !connected ? (
        <Card>
          <EmptyState
            icon={<Inbox size={20} />}
            title="Connect Gmail first"
            description="Resume ingestion reads the connected Gmail inbox. Connect Gmail under Integrations to start ingesting resumes."
            action={
              <Link href="/settings/integrations" className="btn-primary btn-sm">
                Open integrations
              </Link>
            }
          />
        </Card>
      ) : (
        <Card className="p-5">
          {settings.loading ? (
            <Spinner label="Loading sync settings…" />
          ) : (
            <div className="space-y-4">
              <div className="flex flex-wrap items-center gap-3 rounded-[8px] border border-[#ececea] bg-[#fcfcfb] px-3.5 py-2.5 text-[11.5px] text-[#52525b]">
                <span className="badge-green">{settings.data?.connected_email || "Gmail"}</span>
                <StatusBadge status={settings.data?.connector_status} />
                <span className="text-[#8a8a93]">Last synced {formatDateTime(current?.last_synced_at)}</span>
                <span className="text-[#8a8a93]">
                  {!current?.enabled
                    ? "Automatic sync off"
                    : current?.next_sync_at && new Date(current.next_sync_at).getTime() <= Date.now()
                      ? "Next sync overdue"
                      : `Next ${timeFromNow(current?.next_sync_at)}`}
                </span>
                {current?.last_sync_status ? <StatusBadge status={current.last_sync_status} /> : null}
                {current?.consecutive_failures ? (
                  <span className="text-[#9a6a1f]">{current.consecutive_failures} consecutive failures</span>
                ) : null}
              </div>

              {current?.last_sync_error ? <Alert tone="warn">{current.last_sync_error}</Alert> : null}

              <label className="flex items-center gap-2 text-[12px] text-[#52525b]">
                <input
                  type="checkbox"
                  className="accent-[#1f4b3f]"
                  checked={enabled}
                  onChange={(event) => setEnabled(event.target.checked)}
                />
                Automatic ingestion enabled
              </label>

              <div className="grid gap-3 sm:grid-cols-2">
                <div>
                  <label className="label">Interval</label>
                  <select
                    className="input"
                    value={custom ? "custom" : String(interval)}
                    onChange={(event) => {
                      if (event.target.value === "custom") {
                        setCustom(String(interval));
                      } else {
                        setCustom("");
                        setInterval(Number(event.target.value));
                      }
                    }}
                  >
                    {(settings.data?.presets || [15, 30, 60, 180, 360, 1440]).map((preset) => (
                      <option key={preset} value={preset}>
                        Every {preset < 60 ? `${preset} minutes` : `${preset / 60} hour${preset === 60 ? "" : "s"}`}
                      </option>
                    ))}
                    <option value="custom">Custom…</option>
                  </select>
                </div>
                <div>
                  <label className="label">Custom minutes (5–10080)</label>
                  <input
                    className="input"
                    type="number"
                    min={5}
                    max={10080}
                    value={custom}
                    placeholder="e.g. 45"
                    onChange={(event) => setCustom(event.target.value)}
                  />
                </div>
                <div className="sm:col-span-2">
                  <label className="label">Gmail query</label>
                  <input
                    className="input"
                    value={query}
                    onChange={(event) => setQuery(event.target.value)}
                    placeholder="newer_than:2d"
                  />
                  <p className="hint mt-1">
                    Same syntax as the Gmail search box. Keep it narrow to avoid re-reading old threads.
                  </p>
                </div>
              </div>

              <div className="flex justify-end">
                <button className="btn-primary btn-sm" onClick={() => void save()} disabled={busy !== null}>
                  {busy === "save" ? <Loader2 size={13} className="animate-spin" /> : <Save size={13} />}
                  Save settings
                </button>
              </div>
            </div>
          )}
        </Card>
      )}

      <Card>
        <CardHeader
          title="Recent ingestion jobs"
          subtitle="Every run is recorded with its stats and retry state."
        />
        {jobs.loading ? (
          <div className="p-6">
            <Spinner label="Loading jobs…" />
          </div>
        ) : (jobs.data || []).length === 0 ? (
          <EmptyState icon={<Inbox size={20} />} title="No jobs yet" description="Run a sync to create the first job." />
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Status</th>
                  <th>Trigger</th>
                  <th>Attempts</th>
                  <th>Stats</th>
                  <th>Started</th>
                  <th>Error</th>
                </tr>
              </thead>
              <tbody>
                {(jobs.data || []).map((job) => (
                  <tr key={job.id}>
                    <td>
                      <StatusBadge status={job.status} />
                    </td>
                    <td className="text-[11.5px] capitalize text-[#52525b]">{job.trigger}</td>
                    <td className="text-[11.5px] tabular-nums text-[#52525b]">{job.attempts}</td>
                    <td className="max-w-[280px] truncate text-[11px] text-[#8a8a93]">
                      {Object.entries(job.stats || {})
                        .map(([key, value]) => `${key}: ${value}`)
                        .join(" · ") || "—"}
                    </td>
                    <td className="whitespace-nowrap text-[11px] text-[#8a8a93]">
                      {formatDateTime(job.started_at || job.created_at)}
                    </td>
                    <td className="max-w-[240px] truncate text-[11px] text-[#a33a2c]">{job.error_message || "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}
