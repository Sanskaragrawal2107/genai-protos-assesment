import { supabase } from "./supabase";
import {
  addCandidateNoteDirect,
  createJobDirect,
  deleteThreadDirect,
  fetchCandidateAttachments,
  fetchCandidateDetail,
  fetchCandidates,
  fetchChatThreads,
  fetchCommunications,
  fetchDashboardData,
  fetchInterviews,
  fetchJobDetail,
  fetchJobEvaluationsDirect,
  fetchJobs,
  fetchMeDirect,
  fetchPendingActions,
  fetchThreadMessages,
  updateCandidateStatusDirect,
} from "./supabaseQueries";

const isBrowser = typeof window !== "undefined";
const isLocalhost = isBrowser && (window.location.hostname === "localhost" || window.location.hostname === "127.0.0.1");

const configuredApiUrl = process.env.NEXT_PUBLIC_API_URL !== undefined
  ? process.env.NEXT_PUBLIC_API_URL
  : (!isBrowser || isLocalhost ? "http://localhost:8000" : "");

export let API_BASE = (configuredApiUrl || "").replace(/\/$/, "");
let detectedLiveApiBase: string | null = null;

function getCandidateApiBases(): string[] {
  const isLocal = typeof window !== "undefined"
    ? (window.location.hostname === "localhost" || window.location.hostname === "127.0.0.1")
    : true;

  const list: string[] = [];
  if (detectedLiveApiBase !== null) {
    list.push(detectedLiveApiBase);
  }
  list.push(API_BASE);

  if (isLocal) {
    list.push("http://localhost:8000", "http://localhost:8001", "http://127.0.0.1:8000", "http://127.0.0.1:8001");
  }

  return Array.from(new Set(list));
}

async function fetchFromPython(path: string, init: RequestInit): Promise<Response> {
  const bases = getCandidateApiBases();
  let lastErr: any = null;
  for (const base of bases) {
    try {
      const res = await fetch(`${base}${path}`, init);
      detectedLiveApiBase = base;
      API_BASE = base;
      return res;
    } catch (e: any) {
      if (e instanceof DOMException && e.name === "AbortError") throw e;
      lastErr = e;
    }
  }
  throw new ApiError(
    0,
    "Cannot reach the Python AI service. Is the service running on port 8000 or 8001?",
    "Start it with `uvicorn app.main:app` in apps/api."
  );
}

let accessToken: string | null = null;

export function setAccessToken(token: string | null) {
  accessToken = token;
}

export function getAccessToken() {
  return accessToken;
}

export class ApiError extends Error {
  status: number;
  hint?: string;
  constructor(status: number, message: string, hint?: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.hint = hint;
  }
}

type Init = Omit<RequestInit, "body"> & { body?: unknown };

export async function api<T>(path: string, init: Init = {}): Promise<T> {
  const method = (init.method || "GET").toUpperCase();
  const urlObj = new URL(path, "http://localhost");
  const pathname = urlObj.pathname;
  const searchParams = Object.fromEntries(urlObj.searchParams.entries());

  // 1. Direct Supabase Database Reads (Bypasses Python completely for low-latency DB operations)
  if (method === "GET") {
    if (pathname === "/api/dashboard") {
      try {
        return (await fetchDashboardData(supabase)) as T;
      } catch (err: any) {
        throw new ApiError(0, err?.message || "Failed to load dashboard from Supabase");
      }
    }
    if (pathname === "/api/candidates") {
      try {
        return (await fetchCandidates(supabase, undefined, searchParams)) as T;
      } catch (err: any) {
        throw new ApiError(0, err?.message || "Failed to load candidates from Supabase");
      }
    }
    const candAttachmentsMatch = pathname.match(/^\/api\/candidates\/([a-zA-Z0-9_-]+)\/attachments$/);
    if (candAttachmentsMatch) {
      try {
        return (await fetchCandidateAttachments(supabase, candAttachmentsMatch[1])) as T;
      } catch {
        /* fallback to python */
      }
    }
    const candDetailMatch = pathname.match(/^\/api\/candidates\/([a-zA-Z0-9_-]+)$/);
    if (candDetailMatch) {
      try {
        return (await fetchCandidateDetail(supabase, candDetailMatch[1])) as T;
      } catch (err: any) {
        throw new ApiError(0, err?.message || "Failed to load candidate from Supabase");
      }
    }
    if (pathname === "/api/jobs") {
      try {
        return (await fetchJobs(supabase)) as T;
      } catch (err: any) {
        throw new ApiError(0, err?.message || "Failed to load jobs from Supabase");
      }
    }
    const jobEvaluationsMatch = pathname.match(/^\/api\/jobs\/([a-zA-Z0-9_-]+)\/evaluations$/);
    if (jobEvaluationsMatch) {
      try {
        const limit = searchParams.limit ? parseInt(searchParams.limit, 10) : 50;
        return (await fetchJobEvaluationsDirect(supabase, jobEvaluationsMatch[1], limit)) as T;
      } catch {
        /* fallback to python */
      }
    }
    const jobDetailMatch = pathname.match(/^\/api\/jobs\/([a-zA-Z0-9_-]+)$/);
    if (jobDetailMatch) {
      try {
        return (await fetchJobDetail(supabase, jobDetailMatch[1])) as T;
      } catch (err: any) {
        throw new ApiError(0, err?.message || "Failed to load job from Supabase");
      }
    }
    if (pathname === "/api/communications") {
      try {
        const limit = searchParams.limit ? parseInt(searchParams.limit, 10) : 50;
        return (await fetchCommunications(supabase, undefined, limit)) as T;
      } catch (err: any) {
        throw new ApiError(0, err?.message || "Failed to load communications from Supabase");
      }
    }
    if (pathname === "/api/interviews") {
      try {
        return (await fetchInterviews(supabase)) as T;
      } catch (err: any) {
        throw new ApiError(0, err?.message || "Failed to load interviews from Supabase");
      }
    }
    if (pathname === "/api/actions") {
      try {
        const st = searchParams.status || "pending";
        return (await fetchPendingActions(supabase, undefined, st)) as T;
      } catch (err: any) {
        throw new ApiError(0, err?.message || "Failed to load actions from Supabase");
      }
    }
    if (pathname === "/api/me") {
      try {
        return (await fetchMeDirect(supabase)) as T;
      } catch (err: any) {
        throw new ApiError(0, err?.message || "Failed to load current user profile");
      }
    }
    if (pathname === "/api/chat/threads") {
      try {
        const threads = await fetchChatThreads(supabase);
        if (threads && threads.length > 0) return threads as T;
      } catch {
        /* fallback to python service */
      }
    }
    const chatMsgMatch = pathname.match(/^\/api\/chat\/([a-zA-Z0-9_-]+)$/);
    if (chatMsgMatch && chatMsgMatch[1] !== "threads") {
      try {
        const msgs = await fetchThreadMessages(supabase, chatMsgMatch[1]);
        if (msgs && msgs.length > 0) {
          return { thread: { id: chatMsgMatch[1] }, messages: msgs } as T;
        }
      } catch {
        /* fallback to python service */
      }
    }
  }

  // 2. Direct Supabase Database Mutations
  if (method === "POST" || method === "PATCH") {
    const candStatusMatch = pathname.match(/^\/api\/candidates\/([a-zA-Z0-9_-]+)\/status$/);
    if (candStatusMatch && init.body) {
      const b = init.body as any;
      try {
        await updateCandidateStatusDirect(supabase, candStatusMatch[1], b.status, b.reason);
        return { status: b.status } as T;
      } catch (err: any) {
        throw new ApiError(0, err?.message || "Failed to update candidate status");
      }
    }
    const candNoteMatch = pathname.match(/^\/api\/candidates\/([a-zA-Z0-9_-]+)\/notes$/);
    if (candNoteMatch && init.body) {
      const b = init.body as any;
      try {
        await addCandidateNoteDirect(supabase, candNoteMatch[1], b.note, b.next_followup_at);
        return { note: b.note } as T;
      } catch (err: any) {
        throw new ApiError(0, err?.message || "Failed to add candidate note");
      }
    }
    if (pathname === "/api/jobs" && init.body) {
      const b = init.body as any;
      try {
        return (await createJobDirect(supabase, b.title, b.description, b.application_subject_prefix)) as T;
      } catch (err: any) {
        throw new ApiError(0, err?.message || "Failed to create job in Supabase");
      }
    }
  }

  if (method === "DELETE") {
    const delThreadMatch = pathname.match(/^\/api\/chat\/(?:threads\/)?([a-zA-Z0-9_-]+)$/);
    if (delThreadMatch) {
      try {
        await deleteThreadDirect(supabase, delThreadMatch[1]);
        return { success: true } as T;
      } catch (err: any) {
        throw new ApiError(0, err?.message || "Failed to delete chat thread");
      }
    }
  }

  // 3. AI Agent, Evaluation, Upload, & Tooling endpoints call the Python Agent service
  const headers = new Headers(init.headers);
  if (init.body !== undefined && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  if (accessToken) headers.set("Authorization", `Bearer ${accessToken}`);

  let response: Response;
  try {
    response = await fetchFromPython(path, {
      ...init,
      headers,
      cache: "no-store",
      body: init.body === undefined ? undefined : JSON.stringify(init.body),
    });
  } catch (err) {
    if (err instanceof ApiError) throw err;
    throw new ApiError(0, "Cannot reach the Python AI service. Is the service running on port 8000 or 8001?", "Start it with `uvicorn app.main:app` in apps/api.");
  }

  if (!response.ok) {
    let detail = response.statusText || `Request failed (${response.status})`;
    let hint: string | undefined;
    try {
      const payload = await response.json();
      detail = payload.detail || payload.message || detail;
      hint = payload.hint;
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(response.status, detail, hint);
  }
  if (response.status === 204) return undefined as T;
  const text = await response.text();
  return (text ? JSON.parse(text) : undefined) as T;
}

export interface StreamEvent<T = unknown> {
  event: string;
  data: T;
}

function parseSseBlock(raw: string): StreamEvent | null {
  let event = "message";
  const dataLines: string[] = [];
  for (const line of raw.split(/\r?\n/)) {
    if (!line || line.startsWith(":")) continue;
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).replace(/^ /, ""));
  }
  if (dataLines.length === 0) return null;
  const payload = dataLines.join("\n");
  try {
    return { event, data: JSON.parse(payload) };
  } catch {
    return { event, data: payload };
  }
}

export async function apiStream(
  path: string,
  options: { body?: unknown; signal?: AbortSignal; onEvent: (event: StreamEvent) => void },
): Promise<void> {
  const headers = new Headers({ "Content-Type": "application/json", Accept: "text/event-stream" });
  if (accessToken) headers.set("Authorization", `Bearer ${accessToken}`);

  let response: Response;
  try {
    response = await fetchFromPython(path, {
      method: "POST",
      headers,
      body: JSON.stringify(options.body ?? {}),
      cache: "no-store",
      signal: options.signal,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") throw err;
    if (err instanceof ApiError) throw err;
    throw new ApiError(
      0,
      "Cannot reach the Python AI service. Is the service running on port 8000 or 8001?",
      "Start it with `uvicorn app.main:app` in apps/api.",
    );
  }

  if (!response.ok) {
    let detail = response.statusText || `Request failed (${response.status})`;
    let hint: string | undefined;
    try {
      const payload = await response.json();
      detail = payload.detail || payload.message || detail;
      hint = payload.hint;
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(response.status, detail, hint);
  }
  if (!response.body) {
    throw new ApiError(0, "This browser cannot read streamed responses.");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer = (buffer + decoder.decode(value, { stream: true })).replace(/\r\n/g, "\n");
      let boundary = buffer.indexOf("\n\n");
      while (boundary !== -1) {
        const block = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        const parsed = parseSseBlock(block);
        if (parsed) options.onEvent(parsed);
        boundary = buffer.indexOf("\n\n");
      }
    }
    const tail = parseSseBlock(buffer);
    if (tail) options.onEvent(tail);
  } finally {
    reader.releaseLock();
  }
}

export async function downloadFile(path: string, filename: string) {
  const headers = new Headers();
  if (accessToken) headers.set("Authorization", `Bearer ${accessToken}`);
  const response = await fetchFromPython(path, { headers });
  if (!response.ok) {
    throw new ApiError(response.status, "Could not download the file");
  }
  const blob = await response.blob();
  const objectUrl = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = objectUrl;
  anchor.download = filename;
  anchor.target = "_blank";
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(objectUrl), 4000);
}
