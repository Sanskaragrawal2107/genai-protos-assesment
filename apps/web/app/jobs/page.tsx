"use client";

import Link from "next/link";
import { useState } from "react";
import { ArrowRight, Briefcase, Loader2, Plus, Sparkles } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { formatDate } from "@/lib/format";
import type { Job } from "@/lib/types";
import { useFetch } from "@/lib/useFetch";
import {
  Alert,
  Card,
  EmptyState,
  Modal,
  PageHeader,
  StatusBadge,
} from "@/components/ui";

export default function JobsPage() {
  const { data, loading, error, reload } = useFetch<Job[]>("/api/jobs");
  const [open, setOpen] = useState(false);
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [subjectPrefix, setSubjectPrefix] = useState("");
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const suggestedPrefix = title.trim() ? `${title.trim()} Application` : "";
  const trimmedPrefix = subjectPrefix.trim();
  const prefixInvalid = trimmedPrefix.length > 0 && trimmedPrefix.length < 3;

  async function create() {
    setBusy(true);
    setFormError(null);
    try {
      await api("/api/jobs", {
        method: "POST",
        body: {
          title,
          description,
          application_subject_prefix: subjectPrefix.trim() || null,
        },
      });
      setOpen(false);
      setTitle("");
      setDescription("");
      setSubjectPrefix("");
      await reload();
    } catch (err) {
      setFormError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-5">
      <PageHeader
        title="Job openings"
        subtitle="Create a role from a job description and screen every candidate against it."
        meta={
          <span className="meta">
            {loading ? "Loading…" : `${data?.length ?? 0} role${(data?.length ?? 0) === 1 ? "" : "s"}`}
          </span>
        }
        actions={
          <button className="btn-primary" onClick={() => setOpen(true)}>
            <Plus size={14} /> New role
          </button>
        }
      />

      {error ? <Alert tone="error">{error.message}</Alert> : null}

      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
        {loading
          ? [0, 1, 2].map((index) => <div key={index} className="skeleton h-40" />)
          : data?.map((job) => (
              <Link
                key={job.id}
                href={`/jobs/${job.id}`}
                className="group flex flex-col rounded-[10px] border border-[#e7e7e4] bg-white p-5 transition-colors hover:border-[#d8d8d4] hover:bg-[#fcfcfb]"
              >
                <div className="flex items-start justify-between gap-2">
                  <span className="flex h-8 w-8 items-center justify-center rounded-[7px] border border-[#ececea] bg-[#fafaf9] text-[#71717a] transition-colors group-hover:border-[#dfe7e3] group-hover:bg-[#eef3f0] group-hover:text-[#1f4b3f]">
                    <Briefcase size={14} />
                  </span>
                  <StatusBadge status={job.status} />
                </div>
                <h3 className="mt-3 line-clamp-2 text-[13.5px] font-medium leading-5 text-[#18181b]">
                  {job.title}
                </h3>
                {job.application_subject_prefix ? (
                  <p className="mt-1.5 inline-flex w-fit max-w-full items-center gap-1 truncate rounded-[4px] bg-[#f4f4f1] px-1.5 py-0.5 text-[10px] text-[#71717a]">
                    Subject · {job.application_subject_prefix}
                  </p>
                ) : null}
                <p className="mt-1.5 line-clamp-2 text-[11.5px] leading-5 text-[#8a8a93]">
                  {job.description}
                </p>
                <div className="mt-auto flex items-center justify-between border-t border-[#f4f4f1] pt-3 text-[10.5px] text-[#a1a1aa]">
                  <span>{job.candidate_count || 0} evaluated</span>
                  <span className="inline-flex items-center gap-1">
                    {formatDate(job.created_at)}
                    <ArrowRight
                      size={11}
                      className="text-[#d4d4d8] transition-colors group-hover:text-[#1f4b3f]"
                    />
                  </span>
                </div>
              </Link>
            ))}
      </div>

      {!loading && (data?.length ?? 0) === 0 ? (
        <Card>
          <EmptyState
            icon={<Sparkles size={20} />}
            title="No job descriptions yet"
            description="Create a role to start screening candidates."
            action={
              <button className="btn-primary btn-sm" onClick={() => setOpen(true)}>
                <Plus size={13} /> Create the first role
              </button>
            }
          />
        </Card>
      ) : null}

      <Modal
        open={open}
        onClose={() => setOpen(false)}
        title="New role"
        width="max-w-2xl"
        footer={
          <>
            <button className="btn-secondary" onClick={() => setOpen(false)}>
              Cancel
            </button>
            <button
              className="btn-primary"
              onClick={create}
              disabled={
                busy || title.trim().length < 2 || description.trim().length < 20 || prefixInvalid
              }
            >
              {busy ? <Loader2 size={14} className="animate-spin" /> : <Plus size={14} />}
              Create role
            </button>
          </>
        }
      >
        <div className="space-y-3.5">
          <div>
            <label className="label">Role title</label>
            <input
              className="input"
              placeholder="Senior AI Engineer"
              value={title}
              onChange={(event) => setTitle(event.target.value)}
            />
          </div>
          <div>
            <div className="flex items-center justify-between">
              <label className="label">Application subject headline</label>
              {suggestedPrefix ? (
                <button
                  className="link text-[11px]"
                  type="button"
                  onClick={() => setSubjectPrefix(suggestedPrefix)}
                >
                  Use “{suggestedPrefix}”
                </button>
              ) : null}
            </div>
            <input
              className="input"
              placeholder="e.g. AI Engineer Application"
              value={subjectPrefix}
              onChange={(event) => setSubjectPrefix(event.target.value)}
            />
            <p className="hint mt-1.5">
              Only emails whose subject starts with this phrase are imported for this role (Re:/Fwd:
              ignored). Leave blank to import all applications.
            </p>
            {prefixInvalid ? (
              <p className="mt-1 text-[11px] text-[#a33a2c]">
                Use at least 3 characters, or leave it blank.
              </p>
            ) : null}
          </div>
          <div>
            <label className="label">Job description</label>
            <textarea
              className="textarea min-h-[220px] font-mono text-[11px]"
              placeholder="Paste the full job description…"
              value={description}
              onChange={(event) => setDescription(event.target.value)}
            />
          </div>
          {formError ? <Alert tone="error">{formError}</Alert> : null}
        </div>
      </Modal>
    </div>
  );
}
