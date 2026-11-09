"use client";

import { useEffect, useState } from "react";
import { CalendarCheck, Loader2, Video } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { providerLabel } from "@/lib/format";
import { browserTimezone, defaultSlotValue, formatInZone, TIMEZONES, zonedToUtc } from "@/lib/time";
import { Alert, Modal } from "./ui";

interface Candidate {
  id: string;
  full_name: string;
  email?: string | null;
}

interface Slot {
  start: string;
  end: string;
  label: string;
}

interface Props {
  open: boolean;
  onClose: () => void;
  candidate: Candidate;
  timezone?: string;
  onScheduled?: () => void;
}

export function ScheduleInterviewModal({
  open,
  onClose,
  candidate,
  timezone,
  onScheduled,
}: Props) {
  const zone = timezone || browserTimezone();
  const [start, setStart] = useState(defaultSlotValue(24));
  const [duration, setDuration] = useState(45);
  const [selectedZone, setSelectedZone] = useState(zone);
  // No workspace default: the recruiter picks the platform each time.
  const [provider, setProvider] = useState("");
  const [notes, setNotes] = useState("");
  const [autoApprove, setAutoApprove] = useState(false);
  const [busy, setBusy] = useState<"check" | "schedule" | null>(null);
  const [result, setResult] = useState<Record<string, unknown> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [alternatives, setAlternatives] = useState<Slot[]>([]);

  useEffect(() => {
    if (open) {
      setResult(null);
      setError(null);
      setAlternatives([]);
      setStart(defaultSlotValue(24));
    }
  }, [open, candidate.id]);

  async function checkAvailability() {
    setBusy("check");
    setError(null);
    try {
      const payload = await api<{
        available: boolean;
        conflicts: unknown[];
        alternatives: Slot[];
        calendar_connected: boolean;
        teams_connected?: boolean;
        google_meet_connected?: boolean;
      }>("/api/interviews/availability", {
        method: "POST",
        body: {
          start: zonedToUtc(start, selectedZone).toISOString(),
          duration_minutes: duration,
          timezone: selectedZone,
        },
      });
      setAlternatives(payload.alternatives || []);
      if (!payload.available) {
        setError(
          `That slot conflicts with ${payload.conflicts.length} interview already booked in this workspace. Pick one of the suggestions below.`,
        );
      } else {
        const providerConnected =
          provider === "google_meet"
            ? payload.google_meet_connected
            : provider === "none"
              ? true
              : payload.teams_connected ?? payload.calendar_connected;
        setResult({
          status: "available",
          note: providerConnected
            ? "No interview in this workspace overlaps this slot."
            : `No local conflicts. Connect ${providerLabel(provider)} before scheduling to generate the join link.`,
        });
      }
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  }

  async function schedule() {
    if (!provider) {
      setError("Choose a meeting platform (Microsoft Teams or Google Meet) before scheduling.");
      return;
    }
    setBusy("schedule");
    setError(null);
    try {
      const payload = await api<Record<string, unknown>>("/api/interviews/schedule", {
        method: "POST",
        body: {
          candidate_id: candidate.id,
          start: zonedToUtc(start, selectedZone).toISOString(),
          duration_minutes: duration,
          timezone: selectedZone,
          provider,
          notes: notes || null,
          approve: autoApprove,
        },
      });
      setResult(payload);
      if (payload.status === "needs_new_slot") {
        setAlternatives((payload.alternatives as Slot[]) || []);
      }
      onScheduled?.();
    } catch (err) {
      setError(
        err instanceof ApiError ? `${err.message}${err.hint ? ` — ${err.hint}` : ""}` : String(err),
      );
    } finally {
      setBusy(null);
    }
  }

  function applySlot(slot: Slot) {
    const date = new Date(slot.start);
    const pad = (value: number) => String(value).padStart(2, "0");
    setStart(
      `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(
        date.getMinutes(),
      )}`,
    );
    setAlternatives([]);
    setError(null);
  }

  const scheduled = result?.status === "scheduled" || result?.status === "already_scheduled";
  const pending = result?.status === "pending_approval";

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={`Schedule interview — ${candidate.full_name}`}
      footer={
        <>
          <button className="btn-secondary" onClick={onClose}>
            Close
          </button>
          <button className="btn-secondary" onClick={checkAvailability} disabled={busy !== null}>
            {busy === "check" ? <Loader2 size={14} className="animate-spin" /> : <CalendarCheck size={14} />}
            Check availability
          </button>
          <button className="btn-primary" onClick={schedule} disabled={busy !== null}>
            {busy === "schedule" ? <Loader2 size={14} className="animate-spin" /> : <Video size={14} />}
            {autoApprove ? "Schedule now" : "Propose for approval"}
          </button>
        </>
      }
    >
      <div className="space-y-4">
        <div className="rounded-[7px] border border-[#ececea] bg-[#fcfcfb] px-3 py-2 text-[11px] leading-5 text-[#52525b]">
          A real {providerLabel(provider)} room is created and the invitation is emailed through Gmail
          after approval.
        </div>

        <div className="grid gap-3 sm:grid-cols-2">
          <div>
            <label className="label">Date & time</label>
            <input
              className="input"
              type="datetime-local"
              value={start}
              onChange={(event) => setStart(event.target.value)}
            />
          </div>
          <div>
            <label className="label">Timezone</label>
            <select className="input" value={selectedZone} onChange={(event) => setSelectedZone(event.target.value)}>
              {[...new Set([selectedZone, ...TIMEZONES])].map((zone) => (
                <option key={zone} value={zone}>
                  {zone}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label className="label">Duration (minutes)</label>
            <select className="input" value={duration} onChange={(event) => setDuration(Number(event.target.value))}>
              {[30, 45, 60, 90].map((value) => (
                <option key={value} value={value}>
                  {value} min
                </option>
              ))}
            </select>
          </div>
          <div>
            <label className="label">Meeting provider</label>
            <select className="input" value={provider} onChange={(event) => setProvider(event.target.value)}>
              <option value="" disabled>
                Select a platform
              </option>
              <option value="microsoft_teams">Microsoft Teams</option>
              <option value="google_meet">Google Meet</option>
              <option value="none">No video link</option>
            </select>
          </div>
        </div>

        <div>
          <label className="label">Agenda / notes (optional)</label>
          <textarea
            className="textarea"
            rows={3}
            placeholder="System design + LangGraph discussion"
            value={notes}
            onChange={(event) => setNotes(event.target.value)}
          />
        </div>

        <label className="flex items-start gap-2 text-[11.5px] leading-5 text-[#52525b]">
          <input
            type="checkbox"
            className="mt-0.5 accent-[#1f4b3f]"
            checked={autoApprove}
            onChange={(event) => setAutoApprove(event.target.checked)}
          />
          <span>
            Skip approval and book immediately. Otherwise a proposal is queued and you approve it from the
            dashboard.
          </span>
        </label>

        <p className="text-[11px] text-[#8a8a93]">
          Local time interpreted as <span className="font-medium">{selectedZone}</span> →{" "}
          {formatInZone(zonedToUtc(start, selectedZone).toISOString(), selectedZone)}
        </p>

        {error ? <Alert tone="warn">{error}</Alert> : null}

        {alternatives.length > 0 ? (
          <div>
            <p className="stat-label mb-2">Suggested free slots</p>
            <div className="flex flex-wrap gap-2">
              {alternatives.slice(0, 6).map((slot) => (
                <button key={slot.start} className="btn-secondary btn-sm" onClick={() => applySlot(slot)}>
                  {slot.label}
                </button>
              ))}
            </div>
          </div>
        ) : null}

        {scheduled ? (
          <Alert tone="success" title="Interview scheduled">
            <div className="space-y-1">
              {result?.start_time ? <p>{formatInZone(String(result.start_time), selectedZone)}</p> : null}
              <p>Video: {providerLabel(String(result?.provider || provider))}</p>
              {result?.meeting_url ? (
                <a href={String(result.meeting_url)} target="_blank" rel="noreferrer" className="link break-all">
                  {String(result.meeting_url)}
                </a>
              ) : (
                <p>No meeting link was returned - the interview was not marked as scheduled.</p>
              )}
              {Array.isArray(result?.warnings) && result.warnings.length > 0 ? (
                <p className="text-[#9a6a1f]">{(result.warnings as string[]).join(" · ")}</p>
              ) : null}
              <p className="text-[#52525b]">
                A Gmail invitation with the join link is queued for approval in Inbox &amp; outreach.
              </p>
            </div>
          </Alert>
        ) : null}

        {pending ? (
          <Alert tone="info" title="Proposal queued">
            Approve it from the dashboard or the interviews page to create the {providerLabel(provider)} room
            and queue the Gmail invitation.
          </Alert>
        ) : null}

        {result?.status === "available" ? (
          <Alert tone="success">{String(result.note)}</Alert>
        ) : null}
      </div>
    </Modal>
  );
}
