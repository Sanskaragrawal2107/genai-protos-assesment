"use client";

import { X } from "lucide-react";
import type { ReactNode } from "react";
import { pct, statusTone } from "@/lib/format";

export function Card({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <section className={`card ${className}`}>{children}</section>;
}

export function CardHeader({
  title,
  subtitle,
  action,
}: {
  title: ReactNode;
  subtitle?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="card-hd">
      <div className="min-w-0">
        <h2 className="card-title">{title}</h2>
        {subtitle ? <p className="card-sub mt-0.5">{subtitle}</p> : null}
      </div>
      {action ? <div className="shrink-0">{action}</div> : null}
    </div>
  );
}

export function PageHeader({
  title,
  subtitle,
  actions,
  meta,
}: {
  title: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
  meta?: ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-end justify-between gap-4">
      <div className="min-w-0">
        <h1 className="page-title">{title}</h1>
        {subtitle ? <p className="page-sub max-w-2xl">{subtitle}</p> : null}
        {meta ? <div className="mt-2.5 flex flex-wrap items-center gap-2">{meta}</div> : null}
      </div>
      {actions ? <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div> : null}
    </div>
  );
}

export function StatusBadge({ status }: { status?: string | null }) {
  const tone = statusTone(status);
  const cls =
    tone === "green"
      ? "badge-green"
      : tone === "amber"
        ? "badge-amber"
        : tone === "rose"
          ? "badge-rose"
          : tone === "indigo"
            ? "badge-indigo"
            : "badge-slate";

  const dot =
    tone === "green"
      ? "bg-[#2c7a4b]"
      : tone === "amber"
        ? "bg-[#b5832f]"
        : tone === "rose"
          ? "bg-[#b4543f]"
          : tone === "indigo"
            ? "bg-[#1f4b3f]"
            : "bg-[#a1a1aa]";

  return (
    <span className={cls}>
      <span className={`h-1 w-1 rounded-full ${dot}`} />
      {String(status || "unknown")
        .replaceAll("_", " ")
        .replace(/\w+/g, (word) => word.charAt(0).toUpperCase() + word.slice(1).toLowerCase())}
    </span>
  );
}

export function Spinner({ label }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 text-[12px] font-medium text-[#71717a]">
      <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-[#e4e4e7] border-t-[#1f4b3f]" />
      {label || "Loading…"}
    </div>
  );
}

export function EmptyState({
  icon,
  title,
  description,
  action,
}: {
  icon?: ReactNode;
  title: string;
  description?: string;
  action?: ReactNode;
}) {
  return (
    <div className="empty">
      {icon ? (
        <div className="mb-1 flex h-10 w-10 items-center justify-center rounded-[9px] border border-[#ececea] bg-[#fafaf9] text-[#8a8a93]">
          {icon}
        </div>
      ) : null}
      <div>
        <p className="text-[13px] font-semibold text-[#27272a]">{title}</p>
        {description ? (
          <p className="mx-auto mt-1 max-w-sm text-[12px] leading-5 text-[#8a8a93]">{description}</p>
        ) : null}
      </div>
      {action}
    </div>
  );
}

export function Alert({
  tone = "info",
  title,
  children,
}: {
  tone?: "info" | "warn" | "error" | "success";
  title?: string;
  children?: ReactNode;
}) {
  const tones = {
    info: "border-[#dfe7e3] bg-[#f7faf8] text-[#1f4b3f]",
    warn: "border-[#efe3c8] bg-[#fcf9f1] text-[#7a5312]",
    error: "border-[#f0d7d3] bg-[#fdf6f4] text-[#9a3a2c]",
    success: "border-[#d5e6da] bg-[#f5faf6] text-[#17683a]",
  } as const;
  return (
    <div className={`alert ${tones[tone]}`}>
      {title ? <p className="text-[12.5px] font-semibold">{title}</p> : null}
      {children ? (
        <div className={`text-[12px] leading-5 ${title ? "mt-0.5" : ""} opacity-90`}>{children}</div>
      ) : null}
    </div>
  );
}

export function ScoreBar({ label, value }: { label: string; value?: number | null }) {
  const width = pct(value);
  const tone =
    width >= 75 ? "bg-[#1f4b3f]" : width >= 55 ? "bg-[#b5832f]" : "bg-[#b4543f]";
  return (
    <div>
      <div className="flex items-baseline justify-between gap-2 text-[11.5px] text-[#71717a]">
        <span>{label}</span>
        <span className="font-semibold tabular-nums text-[#3f3f46]">{Math.round(value ?? 0)}</span>
      </div>
      <div className="mt-1.5 h-1 w-full overflow-hidden rounded-full bg-[#f0f0ed]">
        <div className={`h-full rounded-full ${tone}`} style={{ width: `${width}%` }} />
      </div>
    </div>
  );
}

export function Modal({
  open,
  onClose,
  title,
  children,
  footer,
  width = "max-w-lg",
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
  footer?: ReactNode;
  width?: string;
}) {
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-[#18181b]/20 p-4 backdrop-blur-[2px]">
      <div
        className={`w-full ${width} overflow-hidden rounded-[12px] border border-[#e4e4e7] bg-white`}
        style={{ boxShadow: "0 24px 60px rgba(24,24,27,0.14)" }}
      >
        <div className="flex items-center justify-between border-b border-[#f0f0ed] px-5 py-3.5">
          <h3 className="text-[14px] font-semibold tracking-[-0.01em] text-[#18181b]">{title}</h3>
          <button
            onClick={onClose}
            className="rounded-[6px] p-1 text-[#a1a1aa] transition-colors hover:bg-[#f4f4f1] hover:text-[#52525b]"
            aria-label="Close"
          >
            <X size={16} />
          </button>
        </div>
        <div className="max-h-[70vh] overflow-y-auto px-5 py-4">{children}</div>
        {footer ? (
          <div className="flex justify-end gap-2 border-t border-[#f0f0ed] bg-[#fcfcfb] px-5 py-3">
            {footer}
          </div>
        ) : null}
      </div>
    </div>
  );
}

export function SectionTitle({ children, hint }: { children: ReactNode; hint?: ReactNode }) {
  return (
    <div className="mb-3 flex items-end justify-between gap-3">
      <h2 className="section-title">{children}</h2>
      {hint ? <span className="meta">{hint}</span> : null}
    </div>
  );
}

const AVATAR_TONES = [
  "bg-[#eef3f0] text-[#1f4b3f]",
  "bg-[#f4f1ea] text-[#6d5b3a]",
  "bg-[#f2f2f4] text-[#52525b]",
  "bg-[#f6efec] text-[#8a4b3f]",
  "bg-[#eef1f4] text-[#44566b]",
];

export function Avatar({ name, size = 34 }: { name?: string | null; size?: number }) {
  const label = (name || "?")
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase())
    .join("");
  let hash = 0;
  for (const char of name || "") hash = (hash * 31 + char.charCodeAt(0)) % 997;
  return (
    <span
      className={`flex shrink-0 items-center justify-center rounded-full border border-black/[0.04] font-semibold ${
        AVATAR_TONES[hash % AVATAR_TONES.length]
      }`}
      style={{ width: size, height: size, fontSize: Math.max(10, size / 2.9) }}
    >
      {label || "?"}
    </span>
  );
}
