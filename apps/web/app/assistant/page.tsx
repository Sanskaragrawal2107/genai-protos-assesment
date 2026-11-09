"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowRight,
  ArrowUp,
  Brain,
  Briefcase,
  Check,
  CheckCheck,
  ChevronDown,
  ChevronRight,
  Code2,
  Loader2,
  Mail,
  MessageSquare,
  PanelLeftClose,
  PanelLeftOpen,
  Plug,
  Plus,
  Search,
  Square,
  Trash2,
  Users,
  X,
  XCircle,
} from "lucide-react";
import { api, apiStream, ApiError } from "@/lib/api";
import { humanize, timeFromNow } from "@/lib/format";
import type { CandidateSummary, ChatPayload, PendingAction, AppConnection, Job } from "@/lib/types";
import { PendingActions } from "@/components/PendingActions";
import { MarkdownMessage } from "@/components/MarkdownMessage";
import { Alert, Avatar, Spinner } from "@/components/ui";

interface Step {
  id: string;
  tool: string;
  label: string;
  status: "running" | "done" | "error";
  detail?: string;
  summary?: string;
  error?: string;
  ms?: number;
}

interface AssistantCandidate {
  id?: string;
  candidate_id?: string;
  full_name?: string;
  current_role?: string | null;
  current_company?: string | null;
  location?: string | null;
  pipeline_status?: string | null;
  match_score?: number | null;
  explanation?: string | null;
  candidate?: Partial<CandidateSummary> | null;
}

interface Turn {
  id: string;
  role: "user" | "assistant";
  text: string;
  jobTitle?: string;
  candidates?: AssistantCandidate[];
  pending?: PendingAction[];
  connections?: AppConnection[];
  intent?: string;
  data?: Record<string, unknown>;
  error?: string;
  steps?: Step[];
  streaming?: boolean;
  progressMs?: number;
  reasoning?: string;
}

interface ThreadSummary {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
}

interface ThreadMessage {
  id?: string;
  role: string;
  content: string;
  payload?: {
    intent?: string;
    data?: Record<string, unknown>;
    candidates?: AssistantCandidate[];
    pending_actions?: PendingAction[];
    connections?: AppConnection[];
    job_id?: string;
    job_title?: string;
  } | null;
  created_at?: string;
}

interface ChatThreadResponse {
  thread: Record<string, unknown>;
  messages: ThreadMessage[];
}

const THREAD_KEY = "recruiter.assistant.thread";

const SUGGESTIONS = [
  {
    category: "Talent sourcing",
    icon: Users,
    query: "Who are the top 5 candidates for Senior AI Engineer? Compare their skills and background.",
  },
  {
    category: "Technical verification",
    icon: Code2,
    query: "Verify GitHub repositories and LeetCode problem ratings for our shortlist candidates.",
  },
  {
    category: "Outreach & interview",
    icon: Mail,
    query: "Draft follow-up emails for screened candidates and prepare Microsoft Teams interview invites.",
  },
  {
    category: "Recruiter approvals",
    icon: CheckCheck,
    query: "Show all pending communications and interview proposals waiting for my review.",
  },
];

export default function AssistantPage() {
  return (
    <Suspense
      fallback={
        <div className="flex min-h-[400px] items-center justify-center p-8">
          <Spinner label="Initializing AI Recruiter..." />
        </div>
      }
    >
      <AssistantInner />
    </Suspense>
  );
}

function AssistantInner() {
  const searchParams = useSearchParams();
  const initialQuery = searchParams.get("q");

  // State
  const [threads, setThreads] = useState<ThreadSummary[]>([]);
  const [threadsLoading, setThreadsLoading] = useState(true);
  const [threadSearch, setThreadSearch] = useState("");
  const [sidebarOpen, setSidebarOpen] = useState(true);

  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState("");
  const [activeThreadId, setActiveThreadId] = useState<string | undefined>();
  const [activeThreadTitle, setActiveThreadTitle] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const [restoring, setRestoring] = useState(false);

  // Job-description context attached to the composer
  const [jobs, setJobs] = useState<Job[]>([]);
  const [jobsLoading, setJobsLoading] = useState(false);
  const [jobPickerOpen, setJobPickerOpen] = useState(false);
  const [jobQuery, setJobQuery] = useState("");
  const [selectedJob, setSelectedJob] = useState<{ id: string; title: string } | null>(null);

  const endRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  const abortControllerRef = useRef<AbortController | null>(null);
  const bootstrapped = useRef(false);

  const streaming = turns.some((turn) => turn.streaming);

  const filteredJobs = useMemo(() => {
    const q = jobQuery.trim().toLowerCase();
    if (!q) return jobs;
    return jobs.filter((job) =>
      `${job.title} ${job.description ?? ""}`.toLowerCase().includes(q),
    );
  }, [jobs, jobQuery]);

  useEffect(() => {
    if (window.matchMedia("(max-width: 767px)").matches) setSidebarOpen(false);
  }, []);

  useEffect(() => {
    const el = inputRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`;
  }, [input]);

  // Auto-scroll when new messages arrive
  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: streaming ? "auto" : "smooth", block: "end" });
  }, [turns, busy, streaming]);

  // Load chat threads list
  const loadThreads = useCallback(async () => {
    try {
      setThreadsLoading(true);
      const res = await api<ThreadSummary[]>("/api/chat/threads");
      if (Array.isArray(res)) {
        setThreads(res);
      }
    } catch {
      // Non-critical: thread history may be empty initially
    } finally {
      setThreadsLoading(false);
    }
  }, []);

  useEffect(() => {
    loadThreads();
  }, [loadThreads]);

  const loadJobs = useCallback(async () => {
    setJobsLoading(true);
    try {
      const res = await api<Job[]>("/api/jobs");
      if (Array.isArray(res)) setJobs(res);
    } catch {
      // Non-critical: the composer still works without a JD attached.
    } finally {
      setJobsLoading(false);
    }
  }, []);

  function openJobPicker() {
    if (jobPickerOpen) {
      setJobPickerOpen(false);
      return;
    }
    setJobPickerOpen(true);
    setJobQuery("");
    void loadJobs();
  }

  function attachJob(job: Job) {
    setSelectedJob({ id: job.id, title: job.title });
    setJobPickerOpen(false);
    setJobQuery("");
    inputRef.current?.focus();
  }

  // Load a specific thread
  const selectThread = useCallback(
    async (threadId: string, threadTitle?: string) => {
      if (busy) return;
      setRestoring(true);
      setActiveThreadId(threadId);
      if (threadTitle) setActiveThreadTitle(threadTitle);
      if (typeof window !== "undefined") window.localStorage.setItem(THREAD_KEY, threadId);

      try {
        const data = await api<ChatThreadResponse | ThreadMessage[]>(`/api/chat/${threadId}`);
        const messages: ThreadMessage[] = Array.isArray(data)
          ? data
          : data?.messages || [];

        setTurns(
          messages.map((message, index) => ({
            id: message.id || `h-${index}`,
            role: message.role === "user" ? "user" : "assistant",
            text: message.content,
            jobTitle: message.payload?.job_title,
            intent: message.payload?.intent,
            candidates: message.payload?.candidates,
            pending: message.payload?.pending_actions,
            connections: message.payload?.connections,
            data: message.payload?.data,
            reasoning:
              typeof message.payload?.data?.reasoning === "string"
                ? (message.payload.data.reasoning as string)
                : undefined,
          })),
        );

        // Restore the JD attached to the most recent user message. If that
        // message had no JD, the chip stays empty (removal is honoured).
        const lastUserMessage = [...messages]
          .reverse()
          .find((message) => message.role === "user");
        setSelectedJob(
          lastUserMessage?.payload?.job_id
            ? {
                id: lastUserMessage.payload.job_id,
                title: lastUserMessage.payload.job_title || "Attached JD",
              }
            : null,
        );
        setJobPickerOpen(false);

        if (!threadTitle && !Array.isArray(data) && (data?.thread as any)?.title) {
          setActiveThreadTitle((data.thread as any).title);
        }
      } catch {
        // If thread cannot be fetched, reset
        setTurns([]);
      } finally {
        setRestoring(false);
      }
    },
    [busy],
  );

  // Initial restore from localStorage or deep link
  useEffect(() => {
    if (initialQuery) return;
    const stored = typeof window !== "undefined" ? window.localStorage.getItem(THREAD_KEY) : null;
    if (stored) {
      void selectThread(stored);
    }
  }, [initialQuery, selectThread]);

  // Handle deep link
  useEffect(() => {
    if (bootstrapped.current) return;
    bootstrapped.current = true;
    if (initialQuery) {
      void handleSend(initialQuery);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialQuery]);

  // Delete thread
  async function handleDeleteThread(threadId: string, e: React.MouseEvent) {
    e.stopPropagation();
    try {
      await api(`/api/chat/${threadId}`, { method: "DELETE" });
      setThreads((current) => current.filter((t) => t.id !== threadId));
      if (activeThreadId === threadId) {
        startNewConversation();
      }
    } catch {
      // optimistic update fallback
      setThreads((current) => current.filter((t) => t.id !== threadId));
      if (activeThreadId === threadId) {
        startNewConversation();
      }
    }
  }

  // Start new conversation
  function startNewConversation() {
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
    }
    if (typeof window !== "undefined") window.localStorage.removeItem(THREAD_KEY);
    setActiveThreadId(undefined);
    setActiveThreadTitle("New Session");
    setTurns([]);
    setInput("");
    setSelectedJob(null);
    setJobPickerOpen(false);
    setBusy(false);
  }

  // Send message
  async function handleSend(messageText: string) {
    const text = messageText.trim();
    if (!text || busy) return;

    setInput("");
    const jobContext = selectedJob;
    const assistantId = `a-${Date.now()}`;
    setTurns((current) => [
      ...current,
      { id: `u-${Date.now()}`, role: "user", text, jobTitle: jobContext?.title },
      { id: assistantId, role: "assistant", text: "", steps: [], streaming: true },
    ]);
    setBusy(true);

    const controller = new AbortController();
    abortControllerRef.current = controller;

    const patch = (fn: (turn: Turn) => Turn) =>
      setTurns((current) => current.map((turn) => (turn.id === assistantId ? fn(turn) : turn)));

    try {
      await apiStream("/api/chat/stream", {
        body: { message: text, thread_id: activeThreadId ?? null, job_id: jobContext?.id ?? null },
        signal: controller.signal,
        onEvent: ({ event, data }) => {
          if (event === "meta") {
            const tid = (data as { thread_id?: string }).thread_id;
            if (tid) {
              setActiveThreadId(tid);
              if (typeof window !== "undefined") window.localStorage.setItem(THREAD_KEY, tid);
              // Refresh threads sidebar so new thread appears immediately
              loadThreads();
            }
            return;
          }
          if (event === "step") {
            const step = data as Step;
            patch((turn) => {
              const steps = [...(turn.steps ?? [])];
              const index = steps.findIndex(
                (item) => item.id === step.id && item.status !== "done" && item.status !== "error",
              );
              if (index >= 0) steps[index] = { ...steps[index], ...step };
              else steps.push(step);
              return { ...turn, steps };
            });
            return;
          }
          if (event === "token") {
            const chunk = (data as { text?: string }).text ?? "";
            if (chunk) patch((turn) => ({ ...turn, text: turn.text + chunk }));
            return;
          }
          if (event === "progress") {
            const elapsed = (data as { elapsed_ms?: number }).elapsed_ms;
            patch((turn) => ({ ...turn, progressMs: typeof elapsed === "number" ? elapsed : turn.progressMs }));
            return;
          }
          if (event === "reasoning") {
            const chunk = (data as { text?: string }).text ?? "";
            if (chunk) patch((turn) => ({ ...turn, reasoning: (turn.reasoning ?? "") + chunk }));
            return;
          }
          if (event === "connect") {
            const connection = data as AppConnection;
            if (connection?.url && connection?.toolkit) {
              patch((turn) => {
                const existing = turn.connections ?? [];
                const already = existing.some((item) => item.toolkit === connection.toolkit);
                return {
                  ...turn,
                  connections: already
                    ? existing.map((item) =>
                        item.toolkit === connection.toolkit ? { ...item, ...connection } : item,
                      )
                    : [...existing, connection],
                };
              });
            }
            return;
          }
          if (event === "done") {
            const payload = data as ChatPayload;
            if (payload.thread_id) {
              setActiveThreadId(payload.thread_id);
              if (typeof window !== "undefined") window.localStorage.setItem(THREAD_KEY, payload.thread_id);
              loadThreads();
            }
            patch((turn) => ({
              ...turn,
              streaming: false,
              progressMs: undefined,
              reasoning:
                payload.reasoning ??
                (typeof payload.data?.reasoning === "string" ? payload.data.reasoning : turn.reasoning),
              text: payload.reply || payload.message || turn.text,
              candidates: payload.candidates,
              pending: payload.pending_actions,
              connections: payload.connections?.length ? payload.connections : turn.connections,
              intent: payload.intent,
              data: payload.data,
            }));
            return;
          }
          if (event === "error") {
            const failure = (data as { message?: string }).message || "Assistant run encountered an issue.";
            patch((turn) => ({ ...turn, streaming: false, error: failure }));
          }
        },
      });
    } catch (err: any) {
      if (err instanceof DOMException && err.name === "AbortError") {
        patch((turn) => ({ ...turn, streaming: false }));
        return;
      }
      patch((turn) => ({
        ...turn,
        streaming: false,
        error:
          err instanceof ApiError
            ? `${err.message}${err.hint ? ` — ${err.hint}` : ""}`
            : err?.message || String(err),
      }));
    } finally {
      patch((turn) => ({ ...turn, streaming: false }));
      setBusy(false);
      abortControllerRef.current = null;
    }
  }

  function handleStop() {
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
      abortControllerRef.current = null;
      setBusy(false);
    }
  }

  async function refreshPending() {
    try {
      const actions = await api<PendingAction[]>("/api/actions?status=pending");
      const pendingSet = new Set((actions || []).map((a) => a.id));
      setTurns((current) =>
        current.map((turn) => {
          if (!turn.pending || turn.pending.length === 0) return turn;
          const remaining = turn.pending.filter((p) => pendingSet.has(p.id));
          return { ...turn, pending: remaining };
        }),
      );
    } catch {
      /* inline list remains */
    }
  }

  // Filtered threads
  const filteredThreads = useMemo(() => {
    if (!threadSearch.trim()) return threads;
    const q = threadSearch.toLowerCase();
    return threads.filter((t) => (t.title || "").toLowerCase().includes(q));
  }, [threads, threadSearch]);

  // Group threads chronologically
  const groupedThreads = useMemo(() => {
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const yesterday = new Date(today);
    yesterday.setDate(yesterday.getDate() - 1);
    const last7Days = new Date(today);
    last7Days.setDate(last7Days.getDate() - 7);

    const groups: {
      today: ThreadSummary[];
      yesterday: ThreadSummary[];
      pastWeek: ThreadSummary[];
      older: ThreadSummary[];
    } = { today: [], yesterday: [], pastWeek: [], older: [] };

    for (const t of filteredThreads) {
      const d = new Date(t.updated_at || t.created_at);
      if (d >= today) groups.today.push(t);
      else if (d >= yesterday) groups.yesterday.push(t);
      else if (d >= last7Days) groups.pastWeek.push(t);
      else groups.older.push(t);
    }

    return groups;
  }, [filteredThreads]);

  return (
    <div className="relative m-3 flex min-h-0 flex-1 overflow-hidden rounded-[10px] border border-[#e7e7e4] bg-white lg:m-0 lg:rounded-none lg:border-0">
      {/* 1. Thread History Drawer (Collapsible) */}
      <aside
        className={`shrink-0 flex-col border-r border-[#ececea] bg-[#fcfcfb] max-md:absolute max-md:inset-y-0 max-md:left-0 max-md:z-20 max-md:shadow-[0_18px_48px_rgba(24,24,27,0.14)] ${
          sidebarOpen ? "flex w-[272px]" : "hidden"
        }`}
      >
        <div className="flex h-14 shrink-0 items-center justify-between border-b border-[#f0f0ed] px-3 pl-4">
          <span className="text-[12px] font-semibold text-[#27272a]">Conversations</span>
          <button
            onClick={() => setSidebarOpen(false)}
            className="rounded-[6px] p-1.5 text-[#a1a1aa] transition-colors hover:bg-[#f0f0ed] hover:text-[#52525b]"
            title="Hide history"
          >
            <PanelLeftClose size={15} />
          </button>
        </div>

        <div className="shrink-0 space-y-2 px-3 py-3">
          <button onClick={startNewConversation} className="btn-secondary w-full justify-start">
            <Plus size={14} />
            New conversation
          </button>
          <div className="relative">
            <Search
              size={13}
              className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-[#a1a1aa]"
            />
            <input
              type="text"
              placeholder="Search history"
              value={threadSearch}
              onChange={(event) => setThreadSearch(event.target.value)}
              className="w-full rounded-[6px] border border-[#ececea] bg-white py-1.5 pl-7.5 pr-2.5 text-[12px] text-[#27272a] placeholder:text-[#a1a1aa] focus:border-[#1f4b3f] focus:outline-none focus:ring-2 focus:ring-[#1f4b3f]/10"
            />
          </div>
        </div>

        <div className="flex-1 overflow-y-auto px-2 pb-3">
          {threadsLoading && threads.length === 0 ? (
            <div className="flex items-center gap-2 px-2 py-6 text-[11.5px] text-[#a1a1aa]">
              <Loader2 size={13} className="animate-spin" />
              Loading history…
            </div>
          ) : null}

          {!threadsLoading && filteredThreads.length === 0 ? (
            <div className="px-3 py-8 text-center">
              <p className="text-[12px] font-medium text-[#52525b]">
                {threadSearch ? "No matching conversations" : "No conversations yet"}
              </p>
              <p className="mt-1 text-[11px] leading-4 text-[#a1a1aa]">
                {threadSearch ? "Try a different search." : "Your chats are saved here automatically."}
              </p>
            </div>
          ) : null}

          <ThreadGroup
            label="Today"
            threads={groupedThreads.today}
            activeThreadId={activeThreadId}
            onSelect={selectThread}
            onDelete={handleDeleteThread}
          />
          <ThreadGroup
            label="Yesterday"
            threads={groupedThreads.yesterday}
            activeThreadId={activeThreadId}
            onSelect={selectThread}
            onDelete={handleDeleteThread}
          />
          <ThreadGroup
            label="Previous 7 days"
            threads={groupedThreads.pastWeek}
            activeThreadId={activeThreadId}
            onSelect={selectThread}
            onDelete={handleDeleteThread}
          />
          <ThreadGroup
            label="Older"
            threads={groupedThreads.older}
            activeThreadId={activeThreadId}
            onSelect={selectThread}
            onDelete={handleDeleteThread}
          />
        </div>

        <div className="shrink-0 border-t border-[#ececea] px-4 py-2.5">
          <p className="text-[10.5px] leading-4 text-[#a1a1aa]">
            {threads.length} saved conversation{threads.length === 1 ? "" : "s"}
          </p>
        </div>
      </aside>

      {sidebarOpen ? (
        <div
          className="absolute inset-0 z-10 bg-[#18181b]/20 md:hidden"
          onClick={() => setSidebarOpen(false)}
        />
      ) : null}

      {/* 2. Main Chat Console Area */}
      <div className="flex min-w-0 flex-1 flex-col overflow-hidden bg-white">
        <div className="flex h-14 shrink-0 items-center justify-between gap-3 border-b border-[#ececea] px-4">
          <div className="flex min-w-0 items-center gap-2.5">
            {!sidebarOpen && (
              <button
                onClick={() => setSidebarOpen(true)}
                title="Show conversation history"
                className="rounded-[6px] border border-[#ececea] p-1.5 text-[#71717a] transition-colors hover:bg-[#fafaf9] hover:text-[#27272a]"
              >
                <PanelLeftOpen size={15} />
              </button>
            )}
            <div className="min-w-0">
              <h2 className="truncate text-[13px] font-semibold text-[#18181b]">
                {activeThreadTitle || "New conversation"}
              </h2>
              <p className="truncate text-[10.5px] leading-4 text-[#a1a1aa]">
                {activeThreadId
                  ? `${turns.filter((turn) => turn.role === "user").length} message${
                      turns.filter((turn) => turn.role === "user").length === 1 ? "" : "s"
                    } · saved automatically`
                  : "Nothing is sent without your approval"}
              </p>
            </div>
          </div>

          <div className="flex shrink-0 items-center gap-1">
            {activeThreadId && (
              <button
                onClick={(event) => handleDeleteThread(activeThreadId, event)}
                title="Delete this conversation"
                className="rounded-[6px] p-1.5 text-[#a1a1aa] transition-colors hover:bg-[#fdf4f2] hover:text-[#a33a2c]"
              >
                <Trash2 size={14} />
              </button>
            )}
            <button onClick={startNewConversation} className="topbar-link">
              <Plus size={14} />
              <span className="hidden sm:inline">New</span>
            </button>
          </div>
        </div>

        {/* Message Stream */}
        <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain">
          <div className="mx-auto w-full max-w-[780px] px-4 py-6 sm:px-6">
            {restoring && (
              <div className="flex items-center justify-center py-16">
                <Spinner label="Loading conversation…" />
              </div>
            )}

            {!restoring && turns.length === 0 && (
              <div className="flex min-h-full flex-col justify-center py-8">
                <div className="mx-auto w-full max-w-[660px]">
                  <span className="flex h-9 w-9 items-center justify-center rounded-[8px] bg-[#1f4b3f] text-[13px] font-bold text-white">
                    R
                  </span>
                  <h3 className="mt-4 text-[21px] font-semibold leading-7 tracking-[-0.02em] text-[#18181b]">
                    Ask about your pipeline
                  </h3>
                  <p className="mt-1.5 text-[13px] leading-6 text-[#71717a]">
                    Search candidates, compare them against a role, prepare outreach, or review
                    approvals.
                  </p>

                  <div className="mt-7 grid gap-2 sm:grid-cols-2">
                    {SUGGESTIONS.map((suggestion) => {
                      const Icon = suggestion.icon;
                      return (
                        <button
                          key={suggestion.query}
                          onClick={() => handleSend(suggestion.query)}
                          className="group flex items-start gap-3 rounded-[9px] border border-[#ececea] bg-white p-3.5 text-left transition-colors hover:border-[#d9d9d4] hover:bg-[#fcfcfb]"
                        >
                          <span className="mt-px flex h-7 w-7 shrink-0 items-center justify-center rounded-[6px] border border-[#ececea] bg-[#fafaf9] text-[#8a8a93] transition-colors group-hover:border-[#d9e5df] group-hover:text-[#1f4b3f]">
                            <Icon size={13} strokeWidth={1.8} />
                          </span>
                          <span className="min-w-0">
                            <span className="block text-[10px] font-semibold uppercase tracking-[0.07em] text-[#a1a1aa]">
                              {suggestion.category}
                            </span>
                            <span className="mt-0.5 block text-[12.5px] font-medium leading-5 text-[#3f3f46]">
                              {suggestion.query}
                            </span>
                          </span>
                          <ArrowRight
                            size={13}
                            className="ml-auto mt-1 shrink-0 text-[#c4c4c8] opacity-0 transition-opacity group-hover:opacity-100"
                          />
                        </button>
                      );
                    })}
                  </div>
                </div>
              </div>
            )}

          {/* Turn rendering */}
          {turns.map((turn) => (
            <div key={turn.id} className="mb-7 last:mb-0">
              {turn.role === "user" ? (
                <div className="flex justify-end">
                  <div className="max-w-[85%] rounded-[9px] border border-[#ececea] bg-[#f7f7f4] px-3.5 py-2.5 text-[13px] leading-6 text-[#27272a] sm:max-w-[75%]">
                    {turn.jobTitle ? (
                      <div className="mb-1.5 flex">
                        <span className="inline-flex max-w-full items-center gap-1 rounded-[5px] border border-[#d9e5df] bg-white px-1.5 py-0.5 text-[10px] font-medium text-[#1f4b3f]">
                          <Briefcase size={10} className="shrink-0" />
                          <span className="min-w-0 truncate">{turn.jobTitle}</span>
                        </span>
                      </div>
                    ) : null}
                    <div className="whitespace-pre-wrap">{turn.text}</div>
                  </div>
                </div>
              ) : (
                <div className="flex gap-3">
                  <span className="mt-px flex h-6 w-6 shrink-0 items-center justify-center rounded-[5px] bg-[#1f4b3f] text-[10px] font-bold text-white">
                    R
                  </span>

                  <div className="min-w-0 flex-1 space-y-2.5">
                    <div className="flex items-center gap-1.5 text-[11px] text-[#a1a1aa]">
                      <span className="font-medium text-[#3f3f46]">Recruiter OS</span>
                    </div>

                    {turn.reasoning ? (
                      <ReasoningDisclosure text={turn.reasoning} streaming={turn.streaming} />
                    ) : null}

                    {turn.steps && turn.steps.length > 0 ? (
                      <ToolExecutionAccordion steps={turn.steps} />
                    ) : null}

                    {turn.text ? (
                      <MarkdownMessage content={turn.text} streaming={turn.streaming} />
                    ) : null}

                    {turn.streaming && !turn.text && !turn.reasoning ? (
                      <div className="flex items-center gap-2 py-1 text-[12px] text-[#8a8a93]">
                        <Loader2 size={13} className="animate-spin" />
                        <span>
                          {turn.progressMs
                            ? `Working… ${Math.round(turn.progressMs / 1000)}s`
                            : "Working…"}
                        </span>
                      </div>
                    ) : null}

                    {turn.error ? <Alert tone="error">{turn.error}</Alert> : null}

                    {turn.connections && turn.connections.length > 0 ? (
                      <div className="space-y-2 pt-1">
                        {turn.connections.map((connection) => (
                          <ConnectionCard key={connection.toolkit} connection={connection} />
                        ))}
                      </div>
                    ) : null}

                    {turn.candidates && turn.candidates.length > 0 ? (
                      <div className="pt-1">
                        <p className="mb-2 text-[10.5px] font-semibold uppercase tracking-[0.07em] text-[#a1a1aa]">
                          Candidates · {turn.candidates.length}
                        </p>
                        <div className="grid gap-2 sm:grid-cols-2">
                          {turn.candidates.slice(0, 8).map((candidate, index) => (
                            <CandidateCard key={candidate.candidate_id || index} candidate={candidate} />
                          ))}
                        </div>
                      </div>
                    ) : null}

                    {turn.pending && turn.pending.length > 0 ? (
                      <div className="overflow-hidden rounded-[9px] border border-[#efe3c8] bg-[#fdfbf5]">
                        <div className="flex items-center justify-between border-b border-[#f2e8d2] px-3.5 py-2">
                          <span className="text-[11.5px] font-semibold text-[#7a5312]">
                            Approvals required · {turn.pending.length}
                          </span>
                          <span className="text-[10.5px] text-[#a1804a]">
                            Nothing is sent yet
                          </span>
                        </div>
                        <div className="p-3">
                          <PendingActions actions={turn.pending} onChanged={refreshPending} />
                        </div>
                      </div>
                    ) : null}
                  </div>
                </div>
              )}
            </div>
          ))}

          <div ref={endRef} />
          </div>
        </div>

        {/* Input Dock Bar */}
        <div className="shrink-0 border-t border-[#ececea] bg-white px-4 py-3 sm:px-6 sm:py-4">
          <form
            onSubmit={(e) => {
              e.preventDefault();
              handleSend(input);
            }}
            className="mx-auto w-full max-w-[780px]"
          >
            <div className="rounded-[12px] border border-[#e0e0dc] bg-white p-2 shadow-[0_1px_2px_rgba(24,24,27,0.04)] transition-colors focus-within:border-[#1f4b3f] focus-within:ring-2 focus-within:ring-[#1f4b3f]/10">
              {selectedJob ? (
                <div className="flex px-2 pt-2">
                  <span className="inline-flex max-w-full items-center gap-1.5 rounded-[6px] border border-[#d9e5df] bg-[#f3f8f5] px-2 py-1 text-[11px] font-medium text-[#1f4b3f]">
                    <Briefcase size={11} className="shrink-0" />
                    <span className="min-w-0 truncate">{selectedJob.title}</span>
                    <button
                      type="button"
                      onClick={() => setSelectedJob(null)}
                      title="Remove JD context"
                      className="ml-0.5 rounded-[4px] p-0.5 text-[#5d8a76] transition-colors hover:bg-[#e3efe8] hover:text-[#173a30]"
                    >
                      <X size={11} />
                    </button>
                  </span>
                </div>
              ) : null}

              <textarea
                rows={1}
                ref={inputRef}
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    handleSend(input);
                  }
                }}
                placeholder="Ask about candidates, a role, or your pipeline…"
                className="block min-h-[38px] w-full resize-none border-0 bg-transparent px-2.5 py-2 text-[13.5px] leading-6 text-[#27272a] placeholder:text-[#a1a1aa] focus:outline-none focus:ring-0"
              />

              <div className="relative flex items-center justify-between gap-2 px-1 pb-0.5 pt-0.5">
                <div className="flex min-w-0 items-center gap-1">
                  <button
                    type="button"
                    onClick={openJobPicker}
                    title="Attach a job description as context for your message"
                    className={`inline-flex h-7 shrink-0 items-center gap-1 rounded-[7px] px-2 text-[11.5px] font-medium transition-colors ${
                      selectedJob
                        ? "bg-[#f3f8f5] text-[#1f4b3f] hover:bg-[#e8f1ec]"
                        : "text-[#71717a] hover:bg-[#f4f4f1] hover:text-[#27272a]"
                    }`}
                  >
                    <Briefcase size={11} />
                    <span>{selectedJob ? "Change JD" : "Add JD"}</span>
                  </button>
                  <p className="hidden truncate pl-1 text-[10.5px] leading-4 text-[#a1a1aa] sm:block">
                    Shift + Enter for a new line
                  </p>
                </div>

                <div className="flex shrink-0 items-center gap-1.5">
                  {busy && (
                    <button
                      type="button"
                      onClick={handleStop}
                      className="inline-flex h-7 items-center gap-1 rounded-[7px] px-2 text-[11.5px] font-medium text-[#71717a] transition-colors hover:bg-[#f4f4f1] hover:text-[#27272a]"
                    >
                      <Square size={9} className="fill-current" />
                      Stop
                    </button>
                  )}
                  <button
                    type="submit"
                    disabled={busy || !input.trim()}
                    title="Send"
                    className="flex h-7 w-8 items-center justify-center rounded-[7px] bg-[#1f4b3f] text-white transition-colors hover:bg-[#163a31] disabled:opacity-40"
                  >
                    {busy ? <Loader2 size={13} className="animate-spin" /> : <ArrowUp size={14} />}
                  </button>
                </div>

                {jobPickerOpen ? (
                  <>
                    <div className="fixed inset-0 z-10" onClick={() => setJobPickerOpen(false)} />
                    <div className="absolute bottom-full left-0 z-20 mb-2 w-[min(320px,calc(100vw-2rem))] overflow-hidden rounded-[10px] border border-[#e0e0dc] bg-white shadow-[0_12px_32px_rgba(24,24,27,0.14)]">
                      <div className="border-b border-[#f0f0ec] p-2">
                        <div className="flex items-center gap-1.5 rounded-[6px] border border-[#ececea] bg-[#fafaf9] px-2">
                          <Search size={12} className="shrink-0 text-[#a1a1aa]" />
                          <input
                            autoFocus
                            value={jobQuery}
                            onChange={(e) => setJobQuery(e.target.value)}
                            onKeyDown={(e) => {
                              if (e.key === "Enter") {
                                e.preventDefault();
                                e.stopPropagation();
                                if (filteredJobs[0]) attachJob(filteredJobs[0]);
                              } else if (e.key === "Escape") {
                                e.preventDefault();
                                setJobPickerOpen(false);
                              }
                            }}
                            placeholder="Search job descriptions…"
                            className="h-7 w-full bg-transparent text-[12px] text-[#27272a] placeholder:text-[#a1a1aa] focus:outline-none"
                          />
                        </div>
                      </div>
                      <div className="max-h-[240px] overflow-y-auto p-1">
                        {jobsLoading ? (
                          <div className="flex items-center justify-center gap-2 py-6 text-[11.5px] text-[#8a8a93]">
                            <Loader2 size={12} className="animate-spin" /> Loading jobs…
                          </div>
                        ) : filteredJobs.length === 0 ? (
                          <div className="px-2 py-5 text-center">
                            <p className="text-[11.5px] text-[#71717a]">
                              {jobs.length === 0 ? "No job descriptions yet." : "No matches."}
                            </p>
                            {jobs.length === 0 ? (
                              <Link
                                href="/jobs"
                                className="mt-1 inline-block text-[11.5px] font-medium text-[#1f4b3f] underline-offset-2 hover:underline"
                              >
                                Create a job description
                              </Link>
                            ) : null}
                          </div>
                        ) : (
                          filteredJobs.map((job) => (
                            <button
                              key={job.id}
                              type="button"
                              onClick={() => attachJob(job)}
                              className={`flex w-full items-start gap-2 rounded-[6px] px-2 py-1.5 text-left transition-colors hover:bg-[#f6f6f3] ${
                                selectedJob?.id === job.id ? "bg-[#f3f8f5]" : ""
                              }`}
                            >
                              <Briefcase size={12} className="mt-0.5 shrink-0 text-[#8a8a93]" />
                              <span className="min-w-0 flex-1">
                                <span className="block truncate text-[12px] font-medium text-[#27272a]">
                                  {job.title}
                                </span>
                                <span className="mt-0.5 block text-[10.5px] text-[#a1a1aa]">
                                  {job.status}
                                  {typeof job.candidate_count === "number"
                                    ? ` · ${job.candidate_count} candidates`
                                    : ""}
                                </span>
                              </span>
                              {selectedJob?.id === job.id ? (
                                <Check size={12} className="mt-0.5 shrink-0 text-[#1f4b3f]" />
                              ) : null}
                            </button>
                          ))
                        )}
                      </div>
                    </div>
                  </>
                ) : null}
              </div>
            </div>
          </form>
        </div>
      </div>
    </div>
  );
}

// Subcomponents

function ThreadGroup({
  label,
  threads,
  activeThreadId,
  onSelect,
  onDelete,
}: {
  label: string;
  threads: ThreadSummary[];
  activeThreadId?: string;
  onSelect: (id: string, title?: string) => void;
  onDelete: (id: string, event: React.MouseEvent) => void;
}) {
  if (threads.length === 0) return null;
  return (
    <div className="mb-1.5">
      <p className="px-2 pb-1 pt-2 text-[10px] font-semibold uppercase tracking-[0.07em] text-[#a1a1aa]">
        {label}
      </p>
      <div className="space-y-px">
        {threads.map((thread) => (
          <ThreadItem
            key={thread.id}
            thread={thread}
            isActive={thread.id === activeThreadId}
            onSelect={() => onSelect(thread.id, thread.title)}
            onDelete={(event) => onDelete(thread.id, event)}
          />
        ))}
      </div>
    </div>
  );
}

function ThreadItem({
  thread,
  isActive,
  onSelect,
  onDelete,
}: {
  thread: ThreadSummary;
  isActive: boolean;
  onSelect: () => void;
  onDelete: (e: React.MouseEvent) => void;
}) {
  return (
    <div
      onClick={onSelect}
      className={`group flex cursor-pointer items-center gap-2 rounded-[6px] px-2 py-1.5 text-[12px] transition-colors ${
        isActive
          ? "bg-[#eef3f0] font-medium text-[#1f4b3f]"
          : "text-[#52525b] hover:bg-[#f4f4f1] hover:text-[#27272a]"
      }`}
    >
      <MessageSquare
        size={12}
        className={`shrink-0 ${isActive ? "text-[#1f4b3f]" : "text-[#c4c4c8]"}`}
      />
      <span className="min-w-0 flex-1 truncate">{thread.title || "Untitled conversation"}</span>
      <span className="shrink-0 text-[10px] tabular-nums text-[#c4c4c8] group-hover:hidden">
        {timeFromNow(thread.updated_at || thread.created_at)}
      </span>
      <button
        onClick={(event) => onDelete(event)}
        title="Delete conversation"
        className="hidden shrink-0 rounded-[4px] p-0.5 text-[#a1a1aa] transition-colors hover:bg-[#f4e6e2] hover:text-[#a33a2c] group-hover:block"
      >
        <Trash2 size={11} />
      </button>
    </div>
  );
}

function ReasoningDisclosure({ text, streaming }: { text: string; streaming?: boolean }) {
  const [open, setOpen] = useState(!!streaming);

  // Open while the model is thinking and fold it away once the answer is ready.
  // A manual toggle afterwards is respected: this effect only fires on change.
  useEffect(() => {
    if (!streaming) setOpen(false);
  }, [streaming]);

  return (
    <div className="overflow-hidden rounded-[8px] border border-[#ececea] bg-[#fbfbfa]">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        className="flex w-full items-center justify-between gap-2 px-2.5 py-1.5 text-left transition-colors hover:bg-[#f7f7f4]"
      >
        <span className="flex min-w-0 items-center gap-1.5">
          <Brain size={11} className="shrink-0 text-[#8a8a93]" />
          <span className="truncate text-[11.5px] font-medium text-[#52525b]">
            {streaming ? "Thinking..." : "Thought process"}
          </span>
        </span>
        <ChevronDown
          size={13}
          className={`shrink-0 text-[#a1a1aa] transition-transform ${open ? "rotate-180" : ""}`}
        />
      </button>

      {open ? (
        <div className="max-h-56 overflow-y-auto border-t border-[#f0f0ed] bg-white px-3 py-2">
          <p className="whitespace-pre-wrap break-words text-[11.5px] leading-[1.65] text-[#71717a]">
            {text}
            {streaming ? (
              <span className="ml-0.5 inline-block h-3 w-1 animate-pulse bg-[#a1a1aa] align-middle" />
            ) : null}
          </p>
        </div>
      ) : null}
    </div>
  );
}

function ToolExecutionAccordion({ steps }: { steps: Step[] }) {
  const [open, setOpen] = useState(false);
  const running = steps.some((step) => step.status === "running");
  const failures = steps.filter((step) => step.status === "error").length;

  return (
    <div className="overflow-hidden rounded-[8px] border border-[#ececea] bg-[#fcfcfb]">
      <button
        onClick={() => setOpen(!open)}
        className="flex w-full items-center justify-between gap-2 px-2.5 py-1.5 text-left transition-colors hover:bg-[#f7f7f4]"
      >
        <div className="flex min-w-0 items-center gap-1.5">
          {running ? (
            <Loader2 size={11} className="shrink-0 animate-spin text-[#1f4b3f]" />
          ) : failures > 0 ? (
            <XCircle size={11} className="shrink-0 text-[#a33a2c]" />
          ) : (
            <Check size={11} className="shrink-0 text-[#3f6d57]" />
          )}
          <span className="truncate text-[11.5px] font-medium text-[#3f3f46]">
            {running
              ? "Working through workspace data…"
              : `${steps.length} step${steps.length === 1 ? "" : "s"} completed`}
          </span>
          {failures > 0 ? (
            <span className="shrink-0 text-[10.5px] text-[#a33a2c]">{failures} failed</span>
          ) : null}
        </div>
        <ChevronDown
          size={13}
          className={`shrink-0 text-[#a1a1aa] transition-transform ${open ? "rotate-180" : ""}`}
        />
      </button>

      {open ? (
        <div className="space-y-1 border-t border-[#f0f0ed] bg-white px-2 py-2">
          {steps.map((step, index) => (
            <div
              key={`${step.id}-${index}`}
              className="flex items-start justify-between gap-2 rounded-[5px] px-1.5 py-1"
            >
              <div className="flex min-w-0 items-start gap-1.5">
                {step.status === "running" ? (
                  <Loader2 size={10} className="mt-0.5 shrink-0 animate-spin text-[#1f4b3f]" />
                ) : step.status === "error" ? (
                  <XCircle size={10} className="mt-0.5 shrink-0 text-[#a33a2c]" />
                ) : (
                  <Check size={10} className="mt-0.5 shrink-0 text-[#3f6d57]" />
                )}
                <div className="min-w-0">
                  <p className="truncate text-[11px] text-[#3f3f46]">{step.label}</p>
                  {step.error ? (
                    <p className="truncate text-[10px] text-[#a33a2c]">{step.error}</p>
                  ) : null}
                </div>
              </div>
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function ConnectionCard({ connection }: { connection: AppConnection }) {
  const name = connection.name || connection.toolkit;
  return (
    <div className="flex items-center gap-3 rounded-[9px] border border-[#d7e3ee] bg-[#f4f8fc] p-3">
      <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-[7px] border border-[#e2ebf4] bg-white">
        {connection.logo ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img src={connection.logo} alt={`${name} logo`} width={18} height={18} />
        ) : (
          <Plug size={15} className="text-[#3f6d57]" />
        )}
      </span>
      <div className="min-w-0 flex-1">
        <p className="text-[12px] font-medium text-[#18181b]">Please connect your {name}</p>
        <p className="mt-0.5 text-[11px] leading-5 text-[#71717a]">
          Authorize {name} once and the assistant can use it automatically. The link expires in 10
          minutes.
        </p>
      </div>
      <a
        href={connection.url}
        target="_blank"
        rel="noopener noreferrer"
        className="inline-flex shrink-0 items-center gap-1 rounded-[6px] bg-[#1f4b3f] px-2.5 py-1.5 text-[11.5px] font-medium text-white transition-colors hover:bg-[#173a30]"
      >
        Connect
        <ArrowRight size={12} />
      </a>
    </div>
  );
}

function CandidateCard({ candidate }: { candidate: AssistantCandidate }) {
  const id = candidate.id || candidate.candidate_id || candidate.candidate?.id;
  const name = candidate.full_name || candidate.candidate?.full_name || "Candidate";
  const role = candidate.current_role || candidate.candidate?.current_role || "Engineering candidate";
  const raw = candidate.match_score ?? null;
  const score = raw === null ? null : Math.round(raw <= 1 ? raw * 100 : raw);
  const bio = candidate.explanation || candidate.current_company || candidate.candidate?.current_company || "";
  const location = candidate.location || candidate.candidate?.location;
  const tone =
    score === null
      ? "border-[#ececea] bg-[#fafaf9] text-[#52525b]"
      : score >= 75
        ? "border-[#d9e6e0] bg-[#eef3f0] text-[#1f4b3f]"
        : score >= 55
          ? "border-[#efe3c8] bg-[#fdfbf5] text-[#7a5312]"
          : "border-[#f1d5d2] bg-[#fdf4f2] text-[#a33a2c]";

  return (
    <div className="flex flex-col rounded-[8px] border border-[#ececea] bg-white p-3 transition-colors hover:border-[#d8d8d4]">
      <div className="flex items-start justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          <Avatar name={name} size={26} />
          <div className="min-w-0">
            <p className="truncate text-[12px] font-medium text-[#18181b]">{name}</p>
            <p className="truncate text-[11px] text-[#71717a]">{role}</p>
          </div>
        </div>
        <span
          className={`shrink-0 rounded-[4px] border px-1.5 py-0.5 text-[10px] font-semibold tabular-nums ${tone}`}
        >
          {score === null ? humanize(candidate.pipeline_status || "candidate") : `${score}%`}
        </span>
      </div>

      {bio ? (
        <p className="mt-2 line-clamp-2 text-[11px] leading-[1.5] text-[#71717a]">{bio}</p>
      ) : null}

      <div className="mt-2.5 flex items-center justify-between border-t border-[#f0f0ed] pt-2">
        <span className="truncate text-[10.5px] text-[#a1a1aa]">{location || "Location not stated"}</span>
        {id ? (
          <Link
            href={`/candidates/${id}`}
            className="inline-flex shrink-0 items-center gap-0.5 text-[11px] font-medium text-[#1f4b3f] hover:underline"
          >
            Open
            <ChevronRight size={11} />
          </Link>
        ) : null}
      </div>
    </div>
  );
}
