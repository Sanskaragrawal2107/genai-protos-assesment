"use client";

import Link from "next/link";
import { Suspense, useMemo, useState } from "react";
import { useSearchParams } from "next/navigation";
import { Search, SlidersHorizontal, Users } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { formatDate, pct, scoreTone } from "@/lib/format";
import { PIPELINE_STATUSES, type CandidateSummary, type PipelineStatus } from "@/lib/types";
import { useFetch } from "@/lib/useFetch";
import {
  Alert,
  Avatar,
  Card,
  EmptyState,
  PageHeader,
  Spinner,
} from "@/components/ui";

function CandidatesView() {
  const params = useSearchParams();
  const [status, setStatus] = useState<string>(params.get("status") || "");
  const [location, setLocation] = useState("");
  const [minExperience, setMinExperience] = useState("");
  const [skills, setSkills] = useState("");
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState<"recent" | "score">("recent");
  const [showFilters, setShowFilters] = useState(true);

  const queryString = useMemo(() => {
    const search = new URLSearchParams();
    search.set("limit", "200");
    if (status) search.set("status", status);
    if (location.trim()) search.set("location", location.trim());
    if (minExperience.trim()) search.set("min_experience_years", minExperience.trim());
    if (skills.trim()) search.set("skills", skills.trim());
    return `/api/candidates?${search.toString()}`;
  }, [status, location, minExperience, skills]);

  const { data, error, loading, reload } = useFetch<CandidateSummary[]>(queryString);
  const [updating, setUpdating] = useState<string | null>(null);
  const [updateError, setUpdateError] = useState<string | null>(null);

  const filtered = useMemo(() => {
    const list = [...(data || [])];
    const needle = query.trim().toLowerCase();
    const matched = needle
      ? list.filter((candidate) =>
          [
            candidate.full_name,
            candidate.email,
            candidate.current_role,
            candidate.current_company,
            candidate.location,
            ...(candidate.skills || []),
          ]
            .filter(Boolean)
            .join(" ")
            .toLowerCase()
            .includes(needle),
        )
      : list;
    if (sort === "score") {
      matched.sort((a, b) => (b.match_score ?? 0) - (a.match_score ?? 0));
    }
    return matched;
  }, [data, query, sort]);

  async function changeStatus(id: string, next: PipelineStatus) {
    setUpdating(id);
    setUpdateError(null);
    try {
      await api(`/api/candidates/${id}/status`, {
        method: "PATCH",
        body: { status: next, reason: "Updated from candidate list" },
      });
      await reload();
    } catch (err) {
      setUpdateError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setUpdating(null);
    }
  }

  const activeFilters = [status, location, minExperience, skills, query].filter(Boolean).length;

  return (
    <div className="space-y-5">
      <PageHeader
        title="Candidates"
        subtitle="Every applicant in the workspace, ranked by their latest match score."
        meta={
          <>
            <span className="meta">
              {loading
                ? "Loading…"
                : `${filtered.length} candidate${filtered.length === 1 ? "" : "s"}${
                    activeFilters ? " matching your filters" : ""
                  }`}
            </span>
            {activeFilters ? (
              <button
                className="link text-[11px]"
                onClick={() => {
                  setStatus("");
                  setLocation("");
                  setMinExperience("");
                  setSkills("");
                  setQuery("");
                }}
              >
                Clear filters
              </button>
            ) : null}
          </>
        }
        actions={
          <>
            <button className="btn-secondary" onClick={() => setShowFilters((value) => !value)}>
              <SlidersHorizontal size={14} /> Filters
            </button>
            <Link href="/assistant" className="btn-primary">
              <Search size={14} /> Ask assistant
            </Link>
          </>
        }
      />

      <Card>
        <div className="space-y-3 px-5 py-4">
          <div className="relative">
            <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-[#a1a1aa]" />
            <input
              className="input pl-9"
              placeholder="Search name, email, company or skill…"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
            />
          </div>
          {showFilters ? (
            <div className="grid gap-3 border-t border-[#f4f4f1] pt-3 sm:grid-cols-2 lg:grid-cols-5">
              <div>
                <label className="label">Stage</label>
                <select
                  className="input"
                  value={status}
                  onChange={(event) => setStatus(event.target.value)}
                >
                  <option value="">All stages</option>
                  {PIPELINE_STATUSES.map((item) => (
                    <option key={item} value={item}>
                      {item.replaceAll("_", " ").toLowerCase()}
                    </option>
                  ))}
                </select>
              </div>
              <div>
                <label className="label">Location</label>
                <input
                  className="input"
                  placeholder="e.g. Bengaluru"
                  value={location}
                  onChange={(event) => setLocation(event.target.value)}
                />
              </div>
              <div>
                <label className="label">Min experience (yrs)</label>
                <input
                  className="input"
                  type="number"
                  min={0}
                  step={0.5}
                  placeholder="3"
                  value={minExperience}
                  onChange={(event) => setMinExperience(event.target.value)}
                />
              </div>
              <div>
                <label className="label">Skills</label>
                <input
                  className="input"
                  placeholder="python, langgraph"
                  value={skills}
                  onChange={(event) => setSkills(event.target.value)}
                />
              </div>
              <div>
                <label className="label">Sort by</label>
                <select
                  className="input"
                  value={sort}
                  onChange={(event) => setSort(event.target.value as "recent" | "score")}
                >
                  <option value="recent">Most recent</option>
                  <option value="score">Best match score</option>
                </select>
              </div>
            </div>
          ) : null}
        </div>
      </Card>

      {error ? (
        <Alert tone="error" title="Could not load candidates">
          {error.message}
        </Alert>
      ) : null}
      {updateError ? <Alert tone="error">{updateError}</Alert> : null}

      <Card>
        {loading ? (
          <div className="p-6">
            <Spinner label="Loading candidates…" />
          </div>
        ) : filtered.length === 0 ? (
          <EmptyState
            icon={<Users size={20} />}
            title="No candidates match"
            description="Applications arrive through the connected Gmail inbox. Try clearing filters or run an inbox sync."
            action={
              <Link href="/settings/sync" className="btn-secondary btn-sm">
                Inbox sync settings
              </Link>
            }
          />
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Candidate</th>
                  <th>Current role</th>
                  <th>Experience</th>
                  <th>Location</th>
                  <th>Top skills</th>
                  <th>Score</th>
                  <th>Stage</th>
                  <th>Added</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((candidate) => {
                  const tone = scoreTone(candidate.match_score);
                  return (
                    <tr key={candidate.id}>
                      <td>
                        <Link
                          href={`/candidates/${candidate.id}`}
                          className="group flex items-center gap-2.5"
                        >
                          <Avatar name={candidate.full_name} size={30} />
                          <span className="min-w-0">
                            <span className="block truncate text-[12.5px] font-medium text-[#18181b] group-hover:text-[#1f4b3f]">
                              {candidate.full_name}
                            </span>
                            <span className="block truncate text-[11px] text-[#8a8a93]">
                              {candidate.email || "No email on file"}
                            </span>
                          </span>
                        </Link>
                      </td>
                      <td className="max-w-[200px]">
                        <p className="truncate text-[12px] text-[#3f3f46]">
                          {candidate.current_role || "—"}
                        </p>
                        <p className="truncate text-[11px] text-[#a1a1aa]">
                          {candidate.current_company || ""}
                        </p>
                      </td>
                      <td className="whitespace-nowrap text-[12px] tabular-nums text-[#52525b]">
                        {candidate.experience_years != null ? `${candidate.experience_years} yrs` : "—"}
                      </td>
                      <td className="max-w-[140px] truncate text-[12px] text-[#52525b]">
                        {candidate.location || "—"}
                      </td>
                      <td>
                        <div className="flex max-w-[220px] flex-wrap gap-1">
                          {(candidate.skills || []).slice(0, 3).map((skill) => (
                            <span key={skill} className="chip">
                              {skill}
                            </span>
                          ))}
                          {(candidate.skills?.length || 0) > 3 ? (
                            <span className="chip">+{(candidate.skills?.length || 0) - 3}</span>
                          ) : null}
                        </div>
                      </td>
                      <td>
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
                        ) : (
                          <span className="text-[11px] text-[#c4c4c8]">—</span>
                        )}
                      </td>
                      <td onClick={(event) => event.stopPropagation()}>
                        <select
                          className="rounded-[5px] border border-transparent bg-transparent px-1.5 py-1 text-[11px] capitalize text-[#52525b] transition-colors hover:border-[#e4e4e7] hover:bg-white focus:border-[#1f4b3f] focus:outline-none focus:ring-2 focus:ring-[#1f4b3f]/10"
                          value={candidate.pipeline_status}
                          disabled={updating === candidate.id}
                          onChange={(event) =>
                            changeStatus(candidate.id, event.target.value as PipelineStatus)
                          }
                        >
                          {PIPELINE_STATUSES.map((item) => (
                            <option key={item} value={item}>
                              {item.replaceAll("_", " ").toLowerCase()}
                            </option>
                          ))}
                        </select>
                      </td>
                      <td className="whitespace-nowrap text-[11px] text-[#a1a1aa]">
                        {formatDate(candidate.created_at)}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        {!loading && filtered.length > 0 ? (
          <div className="flex items-center justify-between border-t border-[#f0f0ed] px-5 py-2.5">
            <span className="meta">
              Showing {filtered.length} of {data?.length ?? filtered.length}
            </span>
            <Link href="/shortlists" className="link text-[11px]">
              View shortlists
            </Link>
          </div>
        ) : null}
      </Card>
    </div>
  );
}

export default function CandidatesPage() {
  return (
    <Suspense fallback={<Spinner label="Loading candidates…" />}>
      <CandidatesView />
    </Suspense>
  );
}
