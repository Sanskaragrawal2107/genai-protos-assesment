"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useMemo, useState } from "react";
import {
  ArrowLeft,
  Briefcase,
  CalendarDays,
  CheckCircle2,
  Download,
  ExternalLink,
  Github,
  Globe,
  Linkedin,
  Loader2,
  Mail,
  MapPin,
  MessageSquare,
  Paperclip,
  Phone,
  Send,
  Sparkles,
  Star,
} from "lucide-react";
import { useWorkspace } from "@/app/providers";
import { api, ApiError, downloadFile } from "@/lib/api";
import { formatDate, formatDateTime, humanize, providerLabel, scoreTone } from "@/lib/format";
import { PIPELINE_STATUSES, type CandidateDetail, type PipelineStatus } from "@/lib/types";
import { useFetch } from "@/lib/useFetch";
import { DraftEmailModal } from "@/components/DraftEmailModal";
import { ScheduleInterviewModal } from "@/components/ScheduleInterviewModal";
import {
  Alert,
  Avatar,
  Card,
  CardHeader,
  EmptyState,
  ScoreBar,
  Spinner,
  StatusBadge,
} from "@/components/ui";

type Tab = "overview" | "screening" | "interviews" | "messages" | "notes";

interface Attachment {
  id: string;
  filename?: string | null;
  mime_type?: string | null;
  size_bytes?: number | null;
  status?: string | null;
  created_at?: string | null;
}

function formatBytes(bytes?: number | null) {
  if (!bytes || bytes <= 0) return "—";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export default function CandidateDetailPage() {
  const params = useParams<{ id: string }>();
  const id = params?.id;
  const { me } = useWorkspace();
  const { data, loading, error, reload } = useFetch<CandidateDetail>(id ? `/api/candidates/${id}` : null);
  const { data: attachments } = useFetch<Attachment[]>(id ? `/api/candidates/${id}/attachments` : null);
  const [tab, setTab] = useState<Tab>("overview");
  const [scheduleOpen, setScheduleOpen] = useState(false);
  const [emailOpen, setEmailOpen] = useState(false);
  const [note, setNote] = useState("");
  const [followup, setFollowup] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<{ tone: "success" | "error"; text: string } | null>(null);

  const skills = useMemo(() => {
    if (!data) return [];
    return Array.from(
      new Set([
        ...(data.skills || []),
        ...(data.programming_languages || []),
        ...(data.frameworks || []),
        ...(data.tools || []),
      ]),
    );
  }, [data]);

  async function changeStatus(next: PipelineStatus) {
    if (!data) return;
    setBusy("status");
    try {
      await api(`/api/candidates/${data.id}/status`, {
        method: "PATCH",
        body: { status: next, reason: "Updated from candidate profile" },
      });
      await reload();
      setNotice({ tone: "success", text: `Moved to ${humanize(next)}.` });
    } catch (err) {
      setNotice({ tone: "error", text: err instanceof ApiError ? err.message : String(err) });
    } finally {
      setBusy(null);
    }
  }

  async function addNote() {
    if (!data || !note.trim()) return;
    setBusy("note");
    try {
      await api(`/api/candidates/${data.id}/notes`, {
        method: "POST",
        body: { note: note.trim(), next_followup_at: followup ? new Date(followup).toISOString() : null },
      });
      setNote("");
      setFollowup("");
      await reload();
    } catch (err) {
      setNotice({ tone: "error", text: err instanceof ApiError ? err.message : String(err) });
    } finally {
      setBusy(null);
    }
  }

  async function openResume() {
    if (!data) return;
    setBusy("resume");
    try {
      await downloadFile(`/api/candidates/${data.id}/resume`, data.resume_filename || "resume.pdf");
    } catch (err) {
      setNotice({ tone: "error", text: err instanceof ApiError ? err.message : String(err) });
    } finally {
      setBusy(null);
    }
  }

  if (loading) {
    return (
      <div className="p-8">
        <Spinner label="Loading candidate…" />
      </div>
    );
  }

  if (error || !data) {
    return (
      <div className="space-y-4">
        <Link href="/candidates" className="btn-ghost btn-sm">
          <ArrowLeft size={14} /> Back to candidates
        </Link>
        <Alert tone="error" title="Candidate not found">
          {error?.message || "This candidate does not exist in your workspace."}
        </Alert>
      </div>
    );
  }

  const latestEvaluation = data.evaluations?.[0];
  const score = data.match_score != null ? Math.round(data.match_score) : null;
  const scoreStyle =
    scoreTone(data.match_score) === "green"
      ? "border-[#d9e6e0] bg-[#eef3f0] text-[#1f4b3f]"
      : scoreTone(data.match_score) === "amber"
        ? "border-[#efe3c8] bg-[#fdfbf5] text-[#7a5312]"
        : "border-[#f0d7d3] bg-[#fdf6f4] text-[#9a3a2c]";

  const tabs: [Tab, string][] = [
    ["overview", "Profile"],
    ["screening", `Screening${latestEvaluation ? ` · ${data.evaluations.length}` : ""}`],
    ["interviews", `Interviews · ${data.interviews.length}`],
    ["messages", `Messages · ${data.communications.length}`],
    ["notes", `Notes · ${data.notes.length}`],
  ];

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between gap-3">
        <Link href="/candidates" className="btn-ghost btn-sm">
          <ArrowLeft size={14} /> Candidates
        </Link>
        <Link
          href={`/assistant?q=${encodeURIComponent(
            `Should we move ${data.full_name} forward? Summarise strengths and gaps.`,
          )}`}
          className="btn-secondary btn-sm"
        >
          <Sparkles size={13} /> Ask about this candidate
        </Link>
      </div>

      {notice ? (
        <Alert tone={notice.tone === "success" ? "success" : "error"}>{notice.text}</Alert>
      ) : null}

      <Card className="p-5">
        <div className="flex flex-col gap-5 lg:flex-row lg:items-start">
          <Avatar name={data.full_name} size={52} />
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <h1 className="text-[22px] font-semibold leading-7 tracking-[-0.02em] text-[#18181b]">
                {data.full_name}
              </h1>
              <StatusBadge status={data.pipeline_status} />
              {score != null ? (
                <span
                  className={`inline-flex items-center rounded-[4px] border px-1.5 py-0.5 text-[11px] font-semibold tabular-nums ${scoreStyle}`}
                >
                  {score} / 100 match
                </span>
              ) : (
                <span className="meta">Not screened yet</span>
              )}
            </div>
            <p className="mt-1 text-[13px] text-[#52525b]">
              {[data.current_role, data.current_company].filter(Boolean).join(" · ") || "—"}
            </p>
            <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1.5 text-[11.5px] text-[#71717a]">
              {data.location ? (
                <span className="inline-flex items-center gap-1">
                  <MapPin size={12} className="text-[#a1a1aa]" /> {data.location}
                </span>
              ) : null}
              {data.experience_years != null ? (
                <span className="inline-flex items-center gap-1">
                  <Briefcase size={12} className="text-[#a1a1aa]" /> {data.experience_years} yrs
                  experience
                </span>
              ) : null}
              {data.email ? (
                <a href={`mailto:${data.email}`} className="inline-flex items-center gap-1 hover:text-[#1f4b3f]">
                  <Mail size={12} className="text-[#a1a1aa]" /> {data.email}
                </a>
              ) : null}
              {data.phone ? (
                <span className="inline-flex items-center gap-1">
                  <Phone size={12} className="text-[#a1a1aa]" /> {data.phone}
                </span>
              ) : null}
              {data.github_url ? (
                <a
                  href={data.github_url}
                  target="_blank"
                  rel="noreferrer"
                  className="inline-flex items-center gap-1 hover:text-[#1f4b3f]"
                >
                  <Github size={12} className="text-[#a1a1aa]" /> GitHub
                </a>
              ) : null}
              {data.leetcode_url ? (
                <a
                  href={data.leetcode_url}
                  target="_blank"
                  rel="noreferrer"
                  className="inline-flex items-center gap-1 hover:text-[#1f4b3f]"
                >
                  <ExternalLink size={12} className="text-[#a1a1aa]" /> LeetCode
                </a>
              ) : null}
              {data.linkedin_url ? (
                <a
                  href={data.linkedin_url}
                  target="_blank"
                  rel="noreferrer"
                  className="inline-flex items-center gap-1 hover:text-[#1f4b3f]"
                >
                  <Linkedin size={12} className="text-[#a1a1aa]" /> LinkedIn
                </a>
              ) : null}
              {data.portfolio_url ? (
                <a
                  href={data.portfolio_url}
                  target="_blank"
                  rel="noreferrer"
                  className="inline-flex items-center gap-1 hover:text-[#1f4b3f]"
                >
                  <Globe size={12} className="text-[#a1a1aa]" /> Portfolio
                </a>
              ) : null}
            </div>
          </div>

          <div className="flex w-full flex-wrap items-center gap-2 lg:w-auto lg:flex-col lg:items-stretch">
            <div className="flex items-center gap-2 lg:justify-between">
              <label className="text-[11.5px] text-[#71717a]">Stage</label>
              <select
                className="input py-1.5 text-[11.5px]"
                value={data.pipeline_status}
                disabled={busy === "status"}
                onChange={(event) => changeStatus(event.target.value as PipelineStatus)}
              >
                {PIPELINE_STATUSES.map((status) => (
                  <option key={status} value={status}>
                    {humanize(status)}
                  </option>
                ))}
              </select>
            </div>
            <button className="btn-primary btn-sm" onClick={() => setScheduleOpen(true)}>
              <CalendarDays size={14} /> Schedule interview
            </button>
            <div className="flex gap-2">
              <button
                className="btn-secondary btn-sm flex-1"
                onClick={() => setEmailOpen(true)}
                disabled={!data.email}
              >
                <Send size={13} /> Draft email
              </button>
              <button
                className="btn-secondary btn-sm flex-1"
                onClick={openResume}
                disabled={!data.resume_storage_path || busy === "resume"}
              >
                {busy === "resume" ? (
                  <Loader2 size={13} className="animate-spin" />
                ) : (
                  <Download size={13} />
                )}
                Resume
              </button>
            </div>
          </div>
        </div>
      </Card>

      <div className="flex gap-5 overflow-x-auto border-b border-[#e7e7e4]">
        {tabs.map(([value, label]) => (
          <button
            key={value}
            onClick={() => setTab(value)}
            className={`-mb-px whitespace-nowrap border-b-2 px-0.5 pb-2.5 text-[12.5px] font-medium transition-colors ${
              tab === value
                ? "border-[#1f4b3f] text-[#1f4b3f]"
                : "border-transparent text-[#71717a] hover:text-[#27272a]"
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      {tab === "overview" ? (
        <div className="grid gap-4 lg:grid-cols-3">
          <div className="space-y-4 lg:col-span-2">
            <Card>
              <CardHeader title="Summary" />
              <div className="px-5 py-4">
                <p className="text-[13px] leading-6 text-[#3f3f46]">
                  {data.summary || "No summary was extracted from the resume."}
                </p>
              </div>
            </Card>

            <Card>
              <CardHeader title="Skills" subtitle={`${skills.length} skills`} />
              <div className="flex flex-wrap gap-1.5 px-5 py-4">
                {skills.length === 0 ? (
                  <p className="text-[12px] text-[#8a8a93]">No skills extracted.</p>
                ) : (
                  skills.map((skill) => (
                    <span key={skill} className="chip">
                      {skill}
                    </span>
                  ))
                )}
              </div>
            </Card>

            <Card>
              <CardHeader title="Projects" subtitle="From the resume" />
              {data.projects.length === 0 ? (
                <EmptyState
                  title="No projects extracted"
                  description="Projects are read from the resume text."
                />
              ) : (
                <ul className="divide-y divide-[#f4f4f1]">
                  {data.projects.map((project, index) => (
                    <li key={index} className="px-5 py-3">
                      <p className="text-[12.5px] font-medium text-[#18181b]">
                        {project.name || "Untitled project"}
                      </p>
                      {project.description ? (
                        <p className="mt-0.5 text-[12px] leading-5 text-[#52525b]">
                          {project.description}
                        </p>
                      ) : null}
                      {(project.technologies || []).length > 0 ? (
                        <div className="mt-1.5 flex flex-wrap gap-1">
                          {(project.technologies || []).map((tech) => (
                            <span key={tech} className="chip">
                              {tech}
                            </span>
                          ))}
                        </div>
                      ) : null}
                    </li>
                  ))}
                </ul>
              )}
            </Card>

            <Card>
              <CardHeader title="Earlier companies & education" />
              <div className="grid gap-4 px-5 py-4 sm:grid-cols-2">
                <div>
                  <p className="stat-label">Companies</p>
                  <ul className="mt-2 space-y-1 text-[12px] text-[#3f3f46]">
                    {data.previous_companies.length === 0 ? (
                      <li className="text-[#a1a1aa]">None listed</li>
                    ) : (
                      data.previous_companies.map((company) => <li key={company}>{company}</li>)
                    )}
                  </ul>
                </div>
                <div>
                  <p className="stat-label">Education</p>
                  <ul className="mt-2 space-y-1 text-[12px] text-[#3f3f46]">
                    {data.education.length === 0 ? (
                      <li className="text-[#a1a1aa]">
                        {data.university
                          ? `${data.university}${data.graduation_year ? ` · ${data.graduation_year}` : ""}`
                          : "None listed"}
                      </li>
                    ) : (
                      data.education.map((item, index) => (
                        <li key={index}>
                          {[item.degree, item.institution, item.graduation_year]
                            .filter(Boolean)
                            .join(" · ")}
                        </li>
                      ))
                    )}
                  </ul>
                </div>
              </div>
            </Card>
          </div>

          <div className="space-y-4">
            <Card>
              <CardHeader title="Details" />
              <dl className="space-y-2.5 px-5 py-4 text-[11.5px]">
                <Row label="Preferred location" value={data.preferred_location} />
                <Row label="Resume file" value={data.resume_filename} />
                <Row label="Added" value={formatDate(data.created_at)} />
                <Row label="Updated" value={formatDateTime(data.updated_at)} />
              </dl>
            </Card>

            <Card>
              <CardHeader title="Applications" subtitle="Source emails" />
              {data.applications.length === 0 ? (
                <p className="px-5 py-4 text-[12px] text-[#8a8a93]">No linked email threads.</p>
              ) : (
                <ul className="divide-y divide-[#f4f4f1]">
                  {data.applications.map((application, index) => (
                    <li key={index} className="px-5 py-2.5">
                      <p className="truncate text-[12px] font-medium text-[#27272a]">
                        {(application.subject as string) || "Application email"}
                      </p>
                      <p className="text-[11px] text-[#a1a1aa]">
                        {formatDate(
                          (application.received_at as string) || (application.created_at as string),
                        )}
                      </p>
                    </li>
                  ))}
                </ul>
              )}
            </Card>

            <Card>
              <CardHeader title="Attachments" subtitle="Received with the application" />
              {!attachments || attachments.length === 0 ? (
                <p className="px-5 py-4 text-[12px] text-[#8a8a93]">No email attachments recorded.</p>
              ) : (
                <ul className="divide-y divide-[#f4f4f1]">
                  {attachments.map((attachment) => (
                    <li
                      key={attachment.id}
                      className="flex items-center gap-2 px-5 py-2.5 text-[11.5px] text-[#52525b]"
                    >
                      <Paperclip size={12} className="shrink-0 text-[#a1a1aa]" />
                      <span className="min-w-0 flex-1 truncate" title={attachment.filename || ""}>
                        {attachment.filename || "attachment"}
                      </span>
                      <span className="shrink-0 text-[10.5px] text-[#a1a1aa]">
                        {formatBytes(attachment.size_bytes)}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </Card>

            <Card>
              <CardHeader title="Status history" />
              {data.status_history.length === 0 ? (
                <p className="px-5 py-4 text-[12px] text-[#8a8a93]">No stage changes yet.</p>
              ) : (
                <ol className="space-y-2.5 px-5 py-4">
                  {data.status_history.map((entry, index) => (
                    <li key={index} className="flex gap-2 text-[11.5px]">
                      <span className="mt-[5px] h-1 w-1 shrink-0 rounded-full bg-[#b9c9c2]" />
                      <div>
                        <p className="text-[#52525b]">
                          {humanize(entry.from_status || "new")} →{" "}
                          <span className="font-medium text-[#27272a]">
                            {humanize(entry.to_status)}
                          </span>
                        </p>
                        <p className="text-[10.5px] text-[#a1a1aa]">
                          {formatDateTime(entry.created_at)}
                        </p>
                      </div>
                    </li>
                  ))}
                </ol>
              )}
            </Card>
          </div>
        </div>
      ) : null}

      {tab === "screening" ? (
        <div className="space-y-4">
          {data.evaluations.length === 0 ? (
            <Card>
              <EmptyState
                icon={<Star size={20} />}
                title="No screening runs yet"
                description="Screen this candidate against a job description to see how they rank."
                action={
                  <Link href="/jobs" className="btn-primary btn-sm">
                    Open jobs
                  </Link>
                }
              />
            </Card>
          ) : (
            data.evaluations.map((evaluation, index) => (
              <Card key={evaluation.id || index}>
                <CardHeader
                  title={`${Math.round(evaluation.match_score)} / 100 match`}
                  subtitle={`Evaluated ${formatDate(evaluation.evaluated_at)}`}
                />
                <div className="grid gap-5 px-5 py-4 lg:grid-cols-2">
                  <div className="space-y-2.5">
                    <ScoreBar label="Skills" value={evaluation.skills_score} />
                    <ScoreBar label="Experience" value={evaluation.experience_score} />
                    <ScoreBar label="Projects" value={evaluation.project_score} />
                    <ScoreBar label="Location" value={evaluation.location_score} />
                    <ScoreBar label="Evidence" value={evaluation.evidence_score} />
                  </div>
                  <div>
                    <p className="text-[12.5px] leading-6 text-[#3f3f46]">{evaluation.explanation}</p>
                    <div className="mt-3 grid gap-3 sm:grid-cols-3">
                      <BulletList title="Strengths" items={evaluation.strengths} tone="green" />
                      <BulletList title="Missing" items={evaluation.missing_requirements} tone="rose" />
                      <BulletList title="Concerns" items={evaluation.concerns} tone="amber" />
                    </div>
                  </div>
                </div>
                {evaluation.verification_evidence ? (
                  <div className="border-t border-[#f0f0ed] px-5 py-3">
                    <p className="stat-label mb-2">Public evidence</p>
                    <div className="flex flex-wrap gap-1.5 text-[11px]">
                      {Object.entries(evaluation.verification_evidence).map(([source, value]) => {
                        const evidence = value as {
                          available?: boolean;
                          reason?: string;
                          profile_url?: string;
                          source_url?: string;
                        };
                        if (!evidence) return null;
                        const url = evidence.profile_url || evidence.source_url;
                        return (
                          <span key={source} className="badge-slate">
                            {source === "github" ? "GitHub" : source === "leetcode" ? "LeetCode" : humanize(source)}:{" "}
                            {evidence.available ? (
                              url ? (
                                <a href={url} target="_blank" rel="noreferrer" className="link">
                                  verified
                                </a>
                              ) : (
                                "verified"
                              )
                            ) : (
                              <span className="text-[#8a8a93]">
                                {evidence.reason || "unavailable"}
                              </span>
                            )}
                          </span>
                        );
                      })}
                    </div>
                  </div>
                ) : null}
              </Card>
            ))
          )}
        </div>
      ) : null}

      {tab === "interviews" ? (
        <Card>
          <CardHeader
            title="Interviews"
            action={
              <button className="btn-primary btn-sm" onClick={() => setScheduleOpen(true)}>
                <CalendarDays size={13} /> Schedule
              </button>
            }
          />
          {data.interviews.length === 0 ? (
            <EmptyState
              icon={<CalendarDays size={20} />}
              title="No interviews yet"
              description="Schedule a real Microsoft Teams meeting; the join link is sent through Gmail."
            />
          ) : (
            <ul className="divide-y divide-[#f4f4f1]">
              {data.interviews.map((interview) => (
                <li
                  key={interview.id}
                  className="flex flex-col gap-2 px-5 py-3.5 sm:flex-row sm:items-center"
                >
                  <div className="min-w-0 flex-1">
                    <p className="text-[12.5px] font-medium text-[#18181b]">
                      {formatDateTime(interview.start_time)}
                    </p>
                    <p className="text-[11px] text-[#8a8a93]">
                      {providerLabel(interview.provider)} · {interview.timezone} · invite{" "}
                      {interview.invite_status}
                    </p>
                  </div>
                  <div className="flex items-center gap-2">
                    <StatusBadge status={interview.status} />
                    {interview.meeting_url ? (
                      <a
                        href={interview.meeting_url}
                        target="_blank"
                        rel="noreferrer"
                        className="btn-secondary btn-sm"
                      >
                        Join
                      </a>
                    ) : null}
                    {interview.calendar_html_link ? (
                      <a
                        href={interview.calendar_html_link}
                        target="_blank"
                        rel="noreferrer"
                        className="btn-ghost btn-sm"
                      >
                        Event
                      </a>
                    ) : null}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </Card>
      ) : null}

      {tab === "messages" ? (
        <Card>
          <CardHeader
            title="Email history"
            action={
              <button
                className="btn-secondary btn-sm"
                onClick={() => setEmailOpen(true)}
                disabled={!data.email}
              >
                <Send size={13} /> New draft
              </button>
            }
          />
          {data.communications.length === 0 ? (
            <EmptyState
              icon={<MessageSquare size={20} />}
              title="No emails yet"
              description="Draft a follow-up — it will wait for your approval."
            />
          ) : (
            <ul className="divide-y divide-[#f4f4f1]">
              {data.communications.map((message) => (
                <li key={message.id} className="px-5 py-3.5">
                  <div className="flex items-center justify-between gap-2">
                    <p className="truncate text-[12.5px] font-medium text-[#18181b]">
                      {message.subject || "(no subject)"}
                    </p>
                    <StatusBadge status={message.status} />
                  </div>
                  <p className="mt-1 whitespace-pre-wrap text-[12px] leading-5 text-[#52525b]">
                    {message.content || ""}
                  </p>
                  <p className="mt-1.5 text-[10.5px] text-[#a1a1aa]">
                    {humanize(message.kind)} · {humanize(message.direction)} ·{" "}
                    {formatDateTime(message.created_at)}
                  </p>
                  {message.error_message ? (
                    <p className="mt-1 text-[11px] text-[#a33a2c]">{message.error_message}</p>
                  ) : null}
                </li>
              ))}
            </ul>
          )}
        </Card>
      ) : null}

      {tab === "notes" ? (
        <div className="grid gap-4 lg:grid-cols-3">
          <Card className="lg:col-span-2">
            <CardHeader title="Recruiter notes" />
            {data.notes.length === 0 ? (
              <EmptyState
                icon={<MessageSquare size={20} />}
                title="No notes yet"
                description="Keep interview feedback and next steps here."
              />
            ) : (
              <ul className="divide-y divide-[#f4f4f1]">
                {data.notes.map((entry, index) => (
                  <li key={index} className="px-5 py-3">
                    <p className="whitespace-pre-wrap text-[12.5px] leading-6 text-[#3f3f46]">
                      {entry.note}
                    </p>
                    <p className="mt-1 text-[10.5px] text-[#a1a1aa]">
                      {formatDateTime(entry.created_at)}
                      {entry.next_followup_at
                        ? ` · follow up ${formatDate(entry.next_followup_at)}`
                        : ""}
                    </p>
                  </li>
                ))}
              </ul>
            )}
          </Card>
          <Card>
            <CardHeader title="Add a note" />
            <div className="space-y-3 px-5 py-4">
              <textarea
                className="textarea"
                placeholder="Strong LangGraph depth, weak on system design…"
                value={note}
                onChange={(event) => setNote(event.target.value)}
              />
              <div>
                <label className="label">Next follow-up (optional)</label>
                <input
                  className="input"
                  type="date"
                  value={followup}
                  onChange={(event) => setFollowup(event.target.value)}
                />
              </div>
              <button
                className="btn-primary w-full"
                onClick={addNote}
                disabled={!note.trim() || busy === "note"}
              >
                {busy === "note" ? (
                  <Loader2 size={14} className="animate-spin" />
                ) : (
                  <CheckCircle2 size={14} />
                )}
                Save note
              </button>
            </div>
          </Card>
        </div>
      ) : null}

      <ScheduleInterviewModal
        open={scheduleOpen}
        onClose={() => setScheduleOpen(false)}
        candidate={{ id: data.id, full_name: data.full_name, email: data.email }}
        onScheduled={() => void reload()}
      />
      <DraftEmailModal
        open={emailOpen}
        onClose={() => setEmailOpen(false)}
        candidate={{ id: data.id, full_name: data.full_name, email: data.email }}
        onSent={() => void reload()}
      />
    </div>
  );
}

function Row({ label, value }: { label: string; value?: string | null }) {
  return (
    <div className="flex items-start justify-between gap-3">
      <dt className="text-[#8a8a93]">{label}</dt>
      <dd className="max-w-[60%] truncate text-right font-medium text-[#27272a]">{value || "—"}</dd>
    </div>
  );
}

function BulletList({ title, items, tone }: { title: string; items?: string[]; tone: string }) {
  const dot =
    tone === "green"
      ? "bg-[#2c7a4b]"
      : tone === "rose"
        ? "bg-[#b4543f]"
        : "bg-[#b5832f]";
  return (
    <div>
      <p className="stat-label">{title}</p>
      {!items || items.length === 0 ? (
        <p className="mt-1 text-[11px] text-[#c4c4c8]">None</p>
      ) : (
        <ul className="mt-1.5 space-y-1">
          {items.map((item, index) => (
            <li key={index} className="flex gap-1.5 text-[11px] leading-[1.55] text-[#52525b]">
              <span className={`mt-[6px] h-1 w-1 shrink-0 rounded-full ${dot}`} />
              {item}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
