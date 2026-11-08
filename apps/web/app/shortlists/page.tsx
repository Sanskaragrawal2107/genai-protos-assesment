"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import { ArrowRight, Mail, Star, Users } from "lucide-react";
import { formatDate, pct, scoreTone } from "@/lib/format";
import type { CandidateSummary } from "@/lib/types";
import { useFetch } from "@/lib/useFetch";
import { Alert, Avatar, Card, EmptyState, PageHeader, Spinner } from "@/components/ui";

const TABS = [
  { key: "SHORTLISTED", label: "Shortlisted" },
  { key: "CONTACTED", label: "Contacted" },
  { key: "INTERVIEW_SCHEDULED", label: "In interview" },
] as const;

export default function ShortlistsPage() {
  const [tab, setTab] = useState<(typeof TABS)[number]["key"]>("SHORTLISTED");
  const { data, error, loading } = useFetch<CandidateSummary[]>(`/api/candidates?limit=200&status=${tab}`);

  const ranked = useMemo(
    () => [...(data || [])].sort((a, b) => (b.match_score ?? 0) - (a.match_score ?? 0)),
    [data],
  );

  return (
    <div className="space-y-5">
      <PageHeader
        title="Shortlists"
        subtitle="Candidates you moved forward, ranked by their latest match score."
        actions={
          <Link href="/jobs" className="btn-secondary">
            Screen against a role <ArrowRight size={14} />
          </Link>
        }
      />

      <div className="flex gap-5 overflow-x-auto border-b border-[#e7e7e4]">
        {TABS.map((item) => (
          <button
            key={item.key}
            onClick={() => setTab(item.key)}
            className={`-mb-px whitespace-nowrap border-b-2 px-0.5 pb-2.5 text-[12.5px] font-medium transition-colors ${
              tab === item.key
                ? "border-[#1f4b3f] text-[#1f4b3f]"
                : "border-transparent text-[#71717a] hover:text-[#27272a]"
            }`}
          >
            {item.label}
          </button>
        ))}
      </div>

      {error ? (
        <Alert tone="error" title="Could not load shortlists">
          {error.message}
        </Alert>
      ) : null}

      {loading ? (
        <Card className="p-6">
          <Spinner label="Loading shortlist…" />
        </Card>
      ) : ranked.length === 0 ? (
        <Card>
          <EmptyState
            icon={<Star size={20} />}
            title="Nothing in this stage yet"
            description="Move strong candidates to Shortlisted to see them here, ranked by score."
            action={
              <Link href="/candidates" className="btn-secondary btn-sm">
                Browse candidates
              </Link>
            }
          />
        </Card>
      ) : (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {ranked.map((candidate) => {
            const tone = scoreTone(candidate.match_score);
            return (
              <Card key={candidate.id} className="flex flex-col gap-3 p-4">
                <div className="flex items-start gap-3">
                  <Avatar name={candidate.full_name} size={36} />
                  <div className="min-w-0 flex-1">
                    <Link
                      href={`/candidates/${candidate.id}`}
                      className="block truncate text-[12.5px] font-medium text-[#18181b] hover:text-[#1f4b3f]"
                    >
                      {candidate.full_name}
                    </Link>
                    <p className="truncate text-[11px] text-[#8a8a93]">
                      {candidate.current_role || "—"}
                      {candidate.current_company ? ` · ${candidate.current_company}` : ""}
                    </p>
                  </div>
                  {candidate.match_score != null ? (
                    <span
                      className={`inline-flex min-w-[38px] justify-center rounded-[4px] border px-1.5 py-0.5 text-[11px] font-semibold tabular-nums ${
                        tone === "green"
                          ? "border-[#d9e6e0] bg-[#eef3f0] text-[#1f4b3f]"
                          : tone === "amber"
                            ? "border-[#efe3c8] bg-[#fdfbf5] text-[#7a5312]"
                            : "border-[#f0d7d3] bg-[#fdf6f4] text-[#9a3a2c]"
                      }`}
                    >
                      {pct(candidate.match_score)}
                    </span>
                  ) : null}
                </div>

                <div className="flex flex-wrap gap-1.5">
                  {(candidate.skills || []).slice(0, 5).map((skill) => (
                    <span key={skill} className="chip">
                      {skill}
                    </span>
                  ))}
                </div>

                <div className="mt-auto flex items-center justify-between border-t border-[#f4f4f1] pt-3 text-[10.5px] text-[#a1a1aa]">
                  <span className="truncate">{candidate.location || "Location not stated"}</span>
                  <span className="shrink-0">{formatDate(candidate.created_at)}</span>
                </div>

                <div className="flex gap-2">
                  <Link href={`/candidates/${candidate.id}`} className="btn-secondary btn-sm flex-1">
                    <Users size={12} /> Profile
                  </Link>
                  <Link
                    href={`/candidates/${candidate.id}#communications`}
                    className="btn-secondary btn-sm flex-1"
                  >
                    <Mail size={12} /> Email
                  </Link>
                </div>
              </Card>
            );
          })}
        </div>
      )}
    </div>
  );
}
