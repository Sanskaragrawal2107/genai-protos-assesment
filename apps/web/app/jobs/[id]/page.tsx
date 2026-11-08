"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useMemo, useState } from "react";
import {
  ArrowLeft,
  Briefcase,
  Check,
  Copy,
  ExternalLink,
  Github,
  Loader2,
  MapPin,
  Pencil,
  Play,
  Star,
} from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { formatDate, pct, scoreTone } from "@/lib/format";
import type {
  CandidateSummary,
  EvaluationRecord,
  EvaluationResult,
  EvaluationRun,
  Job,
  JobRequirements,
  Verification,
} from "@/lib/types";
import { useFetch } from "@/lib/useFetch";
import {
  Alert,
  Avatar,
  Card,
  CardHeader,
  EmptyState,
  ScoreBar,
  SectionTitle,
  Spinner,
  StatusBadge,
} from "@/components/ui";

type JobEvaluation = EvaluationRecord & {
  id?: string;
  candidate_id?: string;
  candidate?: CandidateSummary;
};

export default function JobDetailPage() {
  const params = useParams<{ id: string }>();
  const id = params?.id;
  const { data: job, loading, error, reload } = useFetch<Job>(id ? `/api/jobs/${id}` : null);
  const { data: saved, reload: reloadSaved } = useFetch<JobEvaluation[]>(
    id ? `/api/jobs/${id}/evaluations` : null,
  );

  const [run, setRun] = useState<EvaluationRun | null>(null);
  const [top, setTop] = useState(10);
  const [verify, setVerify] = useState(true);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<{ tone: "success" | "error"; text: string } | null>(null);
  const [ruleEditing, setRuleEditing] = useState(false);
  const [ruleDraft, setRuleDraft] = useState("");
  const [ruleSaving, setRuleSaving] = useState(false);
  const [ruleCopied, setRuleCopied] = useState(false);
  const [ruleError, setRuleError] = useState<string | null>(null);
  const ruleDraftTrimmed = ruleDraft.trim();
  const ruleDraftInvalid = ruleDraftTrimmed.length > 0 && ruleDraftTrimmed.length < 3;

  const requirements = useMemo<JobRequirements | null>(() => {
    if (run?.requirements) return run.requirements;
    const structured = job?.structured_requirements as JobRequirements | undefined;
    return structured && Object.keys(structured).length > 0 ? structured : null;
  }, [run, job]);

  // Prefer the freshly run ranking; otherwise show the persisted evaluations.
  const results = useMemo<EvaluationResult[]>(() => {
    if (run) return run.results;
    return (saved || []).map((evaluation) => ({
      ...evaluation,
      candidate_id: evaluation.candidate_id || evaluation.candidate?.id || "",
      full_name: evaluation.candidate?.full_name || "Unknown candidate",
      method: evaluation.method || "heuristic",
      verification:
        (evaluation.verification_evidence as EvaluationResult["verification"]) || {},
      candidate: evaluation.candidate as CandidateSummary,
    }));
  }, [run, saved]);

  async function evaluate() {
    if (!id) return;
    setBusy(true);
    setNotice(null);
    try {
      const result = await api<EvaluationRun>(
        `/api/jobs/${id}/evaluate?top=${top}&verify=${verify}`,
        { method: "POST" },
      );
      setRun(result);
      await Promise.all([reload(), reloadSaved()]);
      const errorCount = result.errors?.length || 0;
      setNotice({
        tone: "success",
        text: `Screened ${result.evaluated} candidate${result.evaluated === 1 ? "" : "s"}${
          errorCount ? ` · ${errorCount} could not be scored` : ""
        }.`,
      });
    } catch (err) {
      setNotice({ tone: "error", text: err instanceof ApiError ? err.message : String(err) });
    } finally {
      setBusy(false);
    }
  }

  function startRuleEdit() {
    setRuleDraft(job?.application_subject_prefix || "");
    setRuleError(null);
    setRuleEditing(true);
  }

  async function saveRule() {
    if (!id) return;
    setRuleSaving(true);
    setRuleError(null);
    try {
      await api(`/api/jobs/${id}`, {
        method: "PATCH",
        body: { application_subject_prefix: ruleDraft.trim() || null },
      });
      setRuleEditing(false);
      await reload();
    } catch (err) {
      setRuleError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setRuleSaving(false);
    }
  }

  async function copyRule(prefix: string) {
    try {
      await navigator.clipboard.writeText(prefix);
      setRuleCopied(true);
      setTimeout(() => setRuleCopied(false), 2000);
    } catch {
      /* clipboard unavailable */
    }
  }

  if (loading) {
    return (
      <div className="p-8">
        <Spinner label="Loading role…" />
      </div>
    );
  }

  if (error || !job) {
    return (
      <div className="space-y-4">
        <Link href="/jobs" className="btn-ghost btn-sm">
          <ArrowLeft size={14} /> Back to jobs
        </Link>
        <Alert tone="error" title="Role not found">
          {error?.message || "This role does not exist in your workspace."}
        </Alert>
      </div>
    );
  }

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between gap-3">
        <Link href="/jobs" className="btn-ghost btn-sm">
          <ArrowLeft size={14} /> Roles
        </Link>
        <Link
          href={`/assistant?q=${encodeURIComponent(
            `Who are the strongest candidates for ${job.title}? Rank them and explain the gaps.`,
          )}`}
          className="btn-secondary btn-sm"
        >
          <Star size={13} /> Ask about this role
        </Link>
      </div>

      {notice ? (
        <Alert tone={notice.tone === "success" ? "success" : "error"}>{notice.text}</Alert>
      ) : null}

      <Card className="p-5">
        <div className="flex flex-col gap-5 lg:flex-row lg:items-start">
          <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-[8px] border border-[#ececea] bg-[#fafaf9] text-[#71717a]">
            <Briefcase size={17} />
          </span>
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <h1 className="text-[22px] font-semibold leading-7 tracking-[-0.02em] text-[#18181b]">
                {job.title}
              </h1>
              <StatusBadge status={job.status} />
            </div>
            <p className="mt-1 text-[11px] text-[#a1a1aa]">
              Created {formatDate(job.created_at)} · {results.length} evaluated
            </p>
            <p className="mt-3 whitespace-pre-wrap text-[12.5px] leading-6 text-[#52525b]">
              {job.description}
            </p>
          </div>

          <div className="flex w-full flex-col gap-2.5 rounded-[8px] border border-[#ececea] bg-[#fcfcfb] p-3 lg:w-[236px]">
            <div className="flex items-center justify-between gap-2">
              <label className="text-[11.5px] text-[#71717a]">Shortlist size</label>
              <select
                className="input w-[126px] py-1.5 text-[11.5px]"
                value={top}
                onChange={(event) => setTop(Number(event.target.value))}
              >
                {[5, 10, 20, 50].map((value) => (
                  <option key={value} value={value}>
                    Top {value}
                  </option>
                ))}
              </select>
            </div>
            <label className="flex items-start gap-2 text-[11.5px] leading-4 text-[#52525b]">
              <input
                type="checkbox"
                className="mt-0.5 accent-[#1f4b3f]"
                checked={verify}
                onChange={(event) => setVerify(event.target.checked)}
              />
              Verify GitHub and LeetCode evidence
            </label>
            <button className="btn-primary w-full" onClick={() => void evaluate()} disabled={busy}>
              {busy ? <Loader2 size={14} className="animate-spin" /> : <Play size={14} />}
              Screen candidates
            </button>
          </div>
        </div>
      </Card>

      <Card>
        <CardHeader
          title="Application inbox rule"
          subtitle="Inbox sync only imports emails whose subject starts with this phrase, then links them to this role."
          action={
            ruleEditing ? null : (
              <button className="btn-secondary btn-sm" onClick={startRuleEdit}>
                <Pencil size={12} /> Edit
              </button>
            )
          }
        />
        <div className="px-5 py-4">
          {ruleEditing ? (
            <div className="space-y-2">
              <input
                className="input"
                placeholder="e.g. AI Engineer Application"
                value={ruleDraft}
                onChange={(event) => setRuleDraft(event.target.value)}
                autoFocus
              />
              <p className="hint">
                Only subjects starting with this phrase are imported for this role (Re:/Fwd: ignored).
                Leave blank to import all applications.
              </p>
              {ruleDraftInvalid ? (
                <p className="text-[11px] text-[#a33a2c]">Use at least 3 characters, or leave it blank.</p>
              ) : null}
              {ruleError ? <Alert tone="error">{ruleError}</Alert> : null}
              <div className="flex items-center gap-2 pt-0.5">
                <button
                  className="btn-primary btn-sm"
                  onClick={() => void saveRule()}
                  disabled={ruleSaving || ruleDraftInvalid}
                >
                  {ruleSaving ? (
                    <Loader2 size={13} className="animate-spin" />
                  ) : (
                    <Check size={13} />
                  )}
                  Save rule
                </button>
                <button
                  className="btn-secondary btn-sm"
                  onClick={() => setRuleEditing(false)}
                  disabled={ruleSaving}
                >
                  Cancel
                </button>
              </div>
            </div>
          ) : job.application_subject_prefix ? (
            <div className="space-y-2">
              <div className="flex flex-wrap items-center gap-2">
                <code className="rounded-[5px] border border-[#e4e4e7] bg-[#fafaf9] px-2 py-1 text-[11.5px] text-[#27272a]">
                  {job.application_subject_prefix}
                </code>
                <button
                  className="btn-ghost btn-sm"
                  onClick={() => void copyRule(job.application_subject_prefix!)}
                >
                  {ruleCopied ? <Check size={12} /> : <Copy size={12} />}
                  {ruleCopied ? "Copied" : "Copy"}
                </button>
              </div>
              <p className="text-[11.5px] text-[#8a8a93]">
                Expected subject:{" "}
                <span className="font-medium text-[#52525b]">
                  {job.application_subject_prefix} — &lt;Candidate Name&gt;
                </span>
              </p>
            </div>
          ) : (
            <div className="flex flex-wrap items-center justify-between gap-3">
              <p className="text-[12px] text-[#8a8a93]">
                No headline set. Applications are imported without being linked to this role.
              </p>
              <button className="btn-primary btn-sm" onClick={startRuleEdit}>
                <Pencil size={12} /> Set headline
              </button>
            </div>
          )}
        </div>
      </Card>

      <Card>
        <CardHeader
          title="Structured requirements"
          subtitle="Skills, eligibility and responsibilities from the job description"
        />
        {!requirements ? (
          <EmptyState
            icon={<Briefcase size={20} />}
            title="No requirements extracted yet"
            description="Screen candidates to parse the required skills, experience and location from this description."
          />
        ) : (
          <div className="grid gap-5 px-5 py-4 sm:grid-cols-2">
            <RequirementList
              title="Required skills"
              items={requirements.required_skills}
              tone="required"
            />
            <RequirementList
              title="Preferred skills"
              items={requirements.preferred_skills}
              tone="preferred"
            />
            <div>
              <p className="stat-label">Eligibility</p>
              <dl className="mt-2 space-y-2 text-[11.5px]">
                <DetailRow
                  label="Minimum experience"
                  value={
                    requirements.min_experience_years != null
                      ? `${requirements.min_experience_years} yrs`
                      : null
                  }
                />
                <DetailRow
                  label="Preferred experience"
                  value={
                    requirements.preferred_experience_years != null
                      ? `${requirements.preferred_experience_years} yrs`
                      : null
                  }
                />
                <DetailRow label="Location" value={requirements.required_location} />
                <DetailRow
                  label="Other locations"
                  value={(requirements.locations || []).filter(Boolean).join(", ") || null}
                />
                <DetailRow label="Education" value={requirements.education} />
              </dl>
            </div>
            <RequirementList
              title="Responsibilities"
              items={requirements.responsibilities}
              tone="muted"
            />
          </div>
        )}
      </Card>

      <SectionTitle
        hint={
          results.length > 0
            ? `${results.length} result${results.length === 1 ? "" : "s"} · sorted by match score`
            : "No results yet"
        }
      >
        Ranked candidates
      </SectionTitle>

      {run?.errors?.length ? (
        <Alert tone="warn" title={`${run.errors.length} candidate(s) could not be scored`}>
          <ul className="mt-1 space-y-0.5">
            {run.errors.slice(0, 5).map((message, index) => (
              <li key={index}>• {message}</li>
            ))}
          </ul>
        </Alert>
      ) : null}

      {results.length === 0 ? (
        <Card>
          <EmptyState
            icon={<Star size={20} />}
            title="No candidates screened yet"
            description="Screen every candidate against this role to see a ranked shortlist."
            action={
              <button className="btn-primary btn-sm" onClick={() => void evaluate()} disabled={busy}>
                {busy ? <Loader2 size={13} className="animate-spin" /> : <Play size={13} />}
                Screen candidates
              </button>
            }
          />
        </Card>
      ) : (
        <div className="space-y-4">
          {results.map((result, index) => (
            <ResultCard key={result.candidate_id || index} rank={index + 1} result={result} />
          ))}
        </div>
      )}
    </div>
  );
}

function ResultCard({ rank, result }: { rank: number; result: EvaluationResult }) {
  const tone = scoreTone(result.match_score);
  const toneClass =
    tone === "green"
      ? "border-[#d9e6e0] bg-[#eef3f0] text-[#1f4b3f]"
      : tone === "amber"
        ? "border-[#efe3c8] bg-[#fdfbf5] text-[#7a5312]"
        : "border-[#f0d7d3] bg-[#fdf6f4] text-[#9a3a2c]";
  const candidate = result.candidate;

  return (
    <Card>
      <div className="card-hd">
        <div className="flex min-w-0 items-center gap-3">
          <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-[5px] border border-[#ececea] bg-[#fafaf9] text-[11px] font-semibold tabular-nums text-[#71717a]">
            {rank}
          </span>
          <Avatar name={result.full_name} size={32} />
          <div className="min-w-0">
            <Link
              href={`/candidates/${result.candidate_id}`}
              className="block truncate text-[12.5px] font-medium text-[#18181b] hover:text-[#1f4b3f]"
            >
              {result.full_name}
            </Link>
            <p className="truncate text-[11px] text-[#8a8a93]">
              {[candidate?.current_role, candidate?.current_company].filter(Boolean).join(" · ") ||
                "—"}
            </p>
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <span
            className={`inline-flex rounded-[4px] border px-1.5 py-0.5 text-[11px] font-semibold tabular-nums ${toneClass}`}
          >
            {pct(result.match_score)} match
          </span>
        </div>
      </div>

      <div className="grid gap-5 px-5 py-4 lg:grid-cols-2">
        <div>
          <div className="space-y-2.5">
            <ScoreBar label="Skills" value={result.skills_score} />
            <ScoreBar label="Experience" value={result.experience_score} />
            <ScoreBar label="Projects" value={result.project_score} />
            <ScoreBar label="Location" value={result.location_score} />
            <ScoreBar label="Evidence" value={result.evidence_score} />
          </div>
          <div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-[#f4f4f1] pt-2.5 text-[11px] text-[#8a8a93]">
            {candidate?.location ? (
              <span className="inline-flex items-center gap-1">
                <MapPin size={11} /> {candidate.location}
              </span>
            ) : null}
            <span>{candidate?.experience_years ?? 0} yrs experience</span>
            {candidate?.github_url ? (
              <a
                href={candidate.github_url}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-1 hover:text-[#1f4b3f]"
              >
                <Github size={11} /> GitHub
              </a>
            ) : null}
            <Link
              href={`/candidates/${result.candidate_id}`}
              className="inline-flex items-center gap-1 text-[#1f4b3f] hover:underline"
            >
              <ExternalLink size={11} /> Open profile
            </Link>
          </div>
        </div>

        <div>
          <p className="text-[12.5px] leading-6 text-[#3f3f46]">{result.explanation}</p>
          <div className="mt-3 grid gap-3 sm:grid-cols-3">
            <BulletList title="Strengths" items={result.strengths} tone="green" />
            <BulletList title="Missing" items={result.missing_requirements} tone="rose" />
            <BulletList title="Concerns" items={result.concerns} tone="amber" />
          </div>
        </div>
      </div>

      {result.verification && Object.keys(result.verification).length > 0 ? (
        <div className="border-t border-[#f0f0ed] px-5 py-3">
          <p className="stat-label mb-2">Public evidence</p>
          <div className="flex flex-wrap gap-1.5 text-[11px]">
            {(["github", "leetcode"] as const).map((source) => {
              const evidence = result.verification?.[source] as Verification | undefined;
              if (!evidence) return null;
              const url = evidence.profile_url || evidence.source_url;
              return (
                <span key={source} className="badge-slate">
                  {source === "github" ? "GitHub" : "LeetCode"}:{" "}
                  {evidence.available ? (
                    url ? (
                      <a href={url} target="_blank" rel="noreferrer" className="link">
                        verified
                      </a>
                    ) : (
                      "verified"
                    )
                  ) : (
                    <span className="text-[#8a8a93]">{evidence.reason || "unavailable"}</span>
                  )}
                </span>
              );
            })}
          </div>
        </div>
      ) : null}
    </Card>
  );
}

function RequirementList({
  title,
  items,
  tone,
}: {
  title: string;
  items?: string[];
  tone: "required" | "preferred" | "muted";
}) {
  const cls = tone === "required" ? "badge-indigo" : "chip";
  return (
    <div>
      <p className="stat-label">{title}</p>
      {!items || items.length === 0 ? (
        <p className="mt-1.5 text-[11px] text-[#c4c4c8]">None specified</p>
      ) : (
        <div className="mt-1.5 flex flex-wrap gap-1.5">
          {items.map((item) => (
            <span key={item} className={cls}>
              {item}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

function DetailRow({ label, value }: { label: string; value?: string | null }) {
  return (
    <div className="flex items-start justify-between gap-3">
      <dt className="text-[#8a8a93]">{label}</dt>
      <dd className="max-w-[60%] text-right font-medium text-[#27272a]">{value || "—"}</dd>
    </div>
  );
}

function BulletList({ title, items, tone }: { title: string; items?: string[]; tone: string }) {
  const dot = tone === "green" ? "bg-[#2c7a4b]" : tone === "rose" ? "bg-[#b4543f]" : "bg-[#b5832f]";
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
