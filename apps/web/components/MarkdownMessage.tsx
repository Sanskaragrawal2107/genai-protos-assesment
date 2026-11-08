"use client";

import React, { useMemo, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  Calendar,
  Check,
  Clock,
  Copy,
  ExternalLink,
  Mail,
  Video,
} from "lucide-react";

interface MarkdownMessageProps {
  content: string;
  className?: string;
  streaming?: boolean;
}

// Regex to detect Microsoft Teams or Google Meet join links
const MEETING_URL_REGEX =
  /(https:\/\/(?:teams\.microsoft\.com\/l\/meetup-join\/[^\s\)]+|meet\.google\.com\/[^\s\)]+|[a-z0-9-]+\.zoom\.us\/j\/[^\s\)]+))/i;

function normalizeMarkdownText(raw: string): string {
  if (!raw) return "";

  let text = raw;

  // Fix: "[Button Text] (https://...)" -> "[Button Text](https://...)"
  text = text.replace(/\[([^\]]+)\]\s+\((https?:\/\/[^\s\)]+)\)/g, "[$1]($2)");

  // Fix: "[Join Microsoft Teams meeting] https://teams.microsoft.com/..." without parens
  text = text.replace(
    /\[([^\]]+)\]\s+(https?:\/\/[^\s\)]+)/g,
    "[$1]($2)"
  );

  // Auto-format common section titles to standard markdown headers
  const sections = [
    "Interview details",
    "Drafted interview invitation",
    "Notes",
    "Next steps for you",
    "Agenda:",
    "Confirmed meeting details and join link",
    "Next steps (recommended):",
  ];
  for (const sec of sections) {
    const re = new RegExp(`(^|\\n)(?:#{1,3}\\s*)?(${sec})(?:\\s*\\([a-zA-Z\\s]+\\))?\\s*(\\n|:)`, "gi");
    text = text.replace(re, "\n\n### $2$3");
  }

  return text.trim();
}

export function MarkdownMessage({
  content,
  className = "",
  streaming = false,
}: MarkdownMessageProps) {
  const [copiedLink, setCopiedLink] = useState(false);

  // Detect meeting URL for high-visibility join card
  const meetingUrl = useMemo(() => {
    const match = content.match(MEETING_URL_REGEX);
    return match ? match[1] : null;
  }, [content]);

  const isTeams = meetingUrl?.includes("teams.microsoft.com");
  const isMeet = meetingUrl?.includes("meet.google.com");

  const normalized = useMemo(() => normalizeMarkdownText(content), [content]);

  const copyMeetingUrl = () => {
    if (!meetingUrl) return;
    navigator.clipboard.writeText(meetingUrl);
    setCopiedLink(true);
    setTimeout(() => setCopiedLink(false), 2000);
  };

  return (
    <div className={`markdown-message min-w-0 space-y-3 ${className}`}>
      {/* Prominent Meeting Callout Banner if a video link exists */}
      {meetingUrl && (
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-[#cfded7] bg-[#f4f8f6] p-3.5 shadow-xs">
          <div className="flex items-center gap-3 min-w-0">
            <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-[#1f4b3f] text-white shadow-xs">
              <Video size={18} />
            </div>
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <span className="text-[13px] font-semibold text-[#18181b]">
                  {isTeams
                    ? "Microsoft Teams Meeting"
                    : isMeet
                    ? "Google Meet Video Call"
                    : "Online Video Meeting"}
                </span>
                <span className="inline-flex items-center rounded-full bg-[#1f4b3f]/10 px-2 py-0.5 text-[10px] font-medium text-[#1f4b3f]">
                  Ready to join
                </span>
              </div>
              <p className="mt-0.5 truncate text-[11px] text-[#71717a] font-mono">
                {meetingUrl}
              </p>
            </div>
          </div>

          <div className="flex items-center gap-2 shrink-0">
            <button
              type="button"
              onClick={copyMeetingUrl}
              className="inline-flex items-center gap-1.5 rounded-md border border-[#cfded7] bg-white px-2.5 py-1.5 text-[11.5px] font-medium text-[#27272a] shadow-2xs hover:bg-[#fbfbfa] transition-colors"
              title="Copy meeting link"
            >
              {copiedLink ? <Check size={12} className="text-emerald-600" /> : <Copy size={12} />}
              <span>{copiedLink ? "Copied" : "Copy Link"}</span>
            </button>

            <a
              href={meetingUrl}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1.5 rounded-md bg-[#1f4b3f] px-3.5 py-1.5 text-[12px] font-medium text-white shadow-xs hover:bg-[#163a31] transition-colors"
            >
              <span>Join Meeting</span>
              <ExternalLink size={12} />
            </a>
          </div>
        </div>
      )}

      {/* Main Markdown Content */}
      <div className="prose prose-sm max-w-none text-[13.5px] leading-relaxed text-[#27272a] break-words">
        <ReactMarkdown
          remarkPlugins={[remarkGfm]}
          components={{
            h1: ({ children }) => (
              <h1 className="mt-4 mb-2 text-[16px] font-semibold tracking-tight text-[#18181b]">
                {children}
              </h1>
            ),
            h2: ({ children }) => (
              <h2 className="mt-3.5 mb-2 text-[14.5px] font-semibold tracking-tight text-[#18181b]">
                {children}
              </h2>
            ),
            h3: ({ children }) => (
              <h3 className="mt-3.5 mb-1.5 text-[12.5px] font-bold uppercase tracking-wider text-[#18181b]">
                {children}
              </h3>
            ),
            p: ({ children }) => (
              <p className="mb-2.5 last:mb-0 leading-6 text-[#27272a]">{children}</p>
            ),
            ul: ({ children }) => (
              <ul className="my-2 ml-4 list-disc space-y-1 text-[#27272a] marker:text-[#a1a1aa]">
                {children}
              </ul>
            ),
            ol: ({ children }) => (
              <ol className="my-2 ml-4 list-decimal space-y-1 text-[#27272a] marker:text-[#a1a1aa]">
                {children}
              </ol>
            ),
            li: ({ children }) => <li className="leading-6 pl-0.5">{children}</li>,
            strong: ({ children }) => (
              <strong className="font-semibold text-[#18181b] break-all">{children}</strong>
            ),
            em: ({ children }) => <em className="italic text-[#3f3f46]">{children}</em>,
            a: ({ href, children }) => {
              const url = href || "";
              const isVideoLink =
                url.includes("teams.microsoft.com") || url.includes("meet.google.com");

              if (isVideoLink) {
                return (
                  <a
                    href={url}
                    target="_blank"
                    rel="noreferrer"
                    className="inline-flex max-w-full items-center gap-1.5 my-1 px-3 py-1 rounded-md bg-[#1f4b3f] text-white hover:bg-[#163a31] font-medium text-[12px] shadow-xs transition-colors"
                  >
                    <Video size={13} className="shrink-0" />
                    <span className="truncate">{children}</span>
                    <ExternalLink size={11} className="shrink-0 opacity-80" />
                  </a>
                );
              }

              return (
                <a
                  href={url}
                  target="_blank"
                  rel="noreferrer"
                  className="font-medium text-[#1f4b3f] hover:underline underline-offset-2 break-all inline-flex items-center gap-0.5"
                >
                  <span>{children}</span>
                  <ExternalLink size={10} className="opacity-70 inline shrink-0" />
                </a>
              );
            },
            blockquote: ({ children }) => (
              <blockquote className="my-2.5 border-l-2 border-[#1f4b3f] bg-[#fbfbfa] py-1.5 pl-3.5 pr-3 text-[13px] text-[#52525b] rounded-r-md">
                {children}
              </blockquote>
            ),
            code: ({ className: codeClassName, children, ...props }) => {
              const isInline = !codeClassName;
              if (isInline) {
                return (
                  <code
                    className="rounded bg-[#f4f4f5] px-1.5 py-0.5 font-mono text-[12px] text-[#18181b] border border-[#e4e4e7]"
                    {...props}
                  >
                    {children}
                  </code>
                );
              }
              return (
                <pre className="my-2.5 overflow-x-auto rounded-lg border border-[#e4e4e7] bg-[#fbfbfa] p-3 font-mono text-[12px] text-[#18181b]">
                  <code {...props}>{children}</code>
                </pre>
              );
            },
            hr: () => <hr className="my-3.5 border-[#e4e4e7]" />,
          }}
        >
          {normalized}
        </ReactMarkdown>

        {streaming && (
          <span className="ml-1 inline-block h-3.5 w-1 animate-pulse rounded-[1px] bg-[#1f4b3f] align-middle" />
        )}
      </div>
    </div>
  );
}
