"use client";

import { useEffect, useState } from "react";
import { Loader2, Send, Wand2 } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { Alert, Modal } from "./ui";

const KINDS = [
  { value: "follow_up", label: "Follow-up" },
  { value: "shortlist_invite", label: "Shortlist invite" },
  { value: "interview_confirmation", label: "Interview confirmation" },
  { value: "rejection", label: "Polite rejection" },
  { value: "offer", label: "Offer" },
];

interface Candidate {
  id: string;
  full_name: string;
  email?: string | null;
}

interface Props {
  open: boolean;
  onClose: () => void;
  candidate: Candidate;
  onSent?: () => void;
}

interface DraftResponse {
  communication: { id: string; subject?: string | null; content?: string | null; status: string };
  action: { id: string; status: string };
}

export function DraftEmailModal({ open, onClose, candidate, onSent }: Props) {
  const [kind, setKind] = useState("follow_up");
  const [instructions, setInstructions] = useState("");
  const [manual, setManual] = useState(false);
  const [subject, setSubject] = useState("");
  const [body, setBody] = useState("");
  const [draft, setDraft] = useState<DraftResponse | null>(null);
  const [busy, setBusy] = useState<"draft" | "send" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [sent, setSent] = useState(false);

  useEffect(() => {
    if (!open) return;
    setDraft(null);
    setError(null);
    setSent(false);
    setInstructions("");
    setSubject("");
    setBody("");
    setManual(false);
  }, [open, candidate.id]);

  async function createDraft() {
    setBusy("draft");
    setError(null);
    try {
      const payload = await api<DraftResponse>("/api/communications/draft", {
        method: "POST",
        body: {
          candidate_id: candidate.id,
          kind,
          subject: manual ? subject : null,
          body: manual ? body : instructions || null,
          approve: false,
        },
      });
      setDraft(payload);
      setSubject(payload.communication.subject || "");
      setBody(payload.communication.content || "");
    } catch (err) {
      setError(err instanceof ApiError ? `${err.message}${err.hint ? ` — ${err.hint}` : ""}` : String(err));
    } finally {
      setBusy(null);
    }
  }

  async function approveAndSend() {
    if (!draft) return;
    setBusy("send");
    setError(null);
    try {
      const result = await api<{ status: string; result?: { status?: string; error?: string } }>(
        `/api/actions/${draft.action.id}/approve`,
        { method: "POST" },
      );
      if (result.status === "failed") {
        setError(result.result?.error || "The email could not be sent.");
      } else {
        setSent(true);
        onSent?.();
      }
    } catch (err) {
      setError(err instanceof ApiError ? `${err.message}${err.hint ? ` — ${err.hint}` : ""}` : String(err));
    } finally {
      setBusy(null);
    }
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={`Email ${candidate.full_name}`}
      footer={
        <>
          <button className="btn-secondary" onClick={onClose}>
            Close
          </button>
          {draft ? (
            <button className="btn-primary" onClick={approveAndSend} disabled={busy !== null || sent}>
              {busy === "send" ? <Loader2 size={14} className="animate-spin" /> : <Send size={14} />}
              Approve & send
            </button>
          ) : (
            <button className="btn-primary" onClick={createDraft} disabled={busy !== null}>
              {busy === "draft" ? <Loader2 size={14} className="animate-spin" /> : <Wand2 size={14} />}
              Generate draft
            </button>
          )}
        </>
      }
    >
      <div className="space-y-4">
        <div className="flex items-center justify-between rounded-[7px] border border-[#ececea] bg-[#fcfcfb] px-3 py-2 text-[11px] text-[#52525b]">
          <span>To: {candidate.email || "no email on file"}</span>
          <button className="link text-[11px]" onClick={() => setManual((value) => !value)}>
            {manual ? "Use AI draft" : "Write manually"}
          </button>
        </div>

        {!manual ? (
          <>
            <div>
              <label className="label">Email type</label>
              <select className="input" value={kind} onChange={(event) => setKind(event.target.value)}>
                {KINDS.map((item) => (
                  <option key={item.value} value={item.value}>
                    {item.label}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label className="label">Extra instructions (optional)</label>
              <textarea
                className="textarea"
                rows={3}
                placeholder="Mention the hybrid role in Bengaluru and ask about notice period."
                value={instructions}
                onChange={(event) => setInstructions(event.target.value)}
              />
            </div>
          </>
        ) : (
          <>
            <div>
              <label className="label">Subject</label>
              <input className="input" value={subject} onChange={(event) => setSubject(event.target.value)} />
            </div>
            <div>
              <label className="label">Body</label>
              <textarea
                className="textarea min-h-[180px]"
                value={body}
                onChange={(event) => setBody(event.target.value)}
              />
            </div>
          </>
        )}

        {draft ? (
          <div className="overflow-hidden rounded-[8px] border border-[#e4e4e7]">
            <div className="border-b border-[#f0f0ed] px-3 py-2">
              <p className="text-[10.5px] font-medium uppercase tracking-[0.06em] text-[#a1a1aa]">Subject</p>
              <input
                className="mt-1 w-full border-0 p-0 text-[12.5px] font-medium text-[#18181b] focus:outline-none"
                value={subject}
                onChange={(event) => setSubject(event.target.value)}
              />
            </div>
            <div className="px-3 py-2">
              <p className="text-[10.5px] font-medium uppercase tracking-[0.06em] text-[#a1a1aa]">Body</p>
              <textarea
                className="mt-1 min-h-[200px] w-full resize-y border-0 p-0 text-[12.5px] leading-6 text-[#3f3f46] focus:outline-none"
                value={body}
                onChange={(event) => setBody(event.target.value)}
              />
            </div>
            <div className="border-t border-[#f0f0ed] bg-[#fcfcfb] px-3 py-2 text-[10.5px] leading-4 text-[#8a8a93]">
              The email is sent only after you approve it.
            </div>
          </div>
        ) : null}

        {error ? <Alert tone="error">{error}</Alert> : null}
        {sent ? <Alert tone="success">Email sent through the connected Gmail account.</Alert> : null}
      </div>
    </Modal>
  );
}
