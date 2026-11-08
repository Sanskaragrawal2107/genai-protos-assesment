export function formatDate(value?: string | null, opts?: Intl.DateTimeFormatOptions) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString(undefined, {
    day: "2-digit",
    month: "short",
    year: "numeric",
    ...opts,
  });
}

export function formatDateTime(value?: string | null) {
  return formatDate(value, { hour: "2-digit", minute: "2-digit" });
}

export function timeFromNow(value?: string | null) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  const diff = date.getTime() - Date.now();
  const abs = Math.abs(diff);
  const minutes = Math.round(abs / 60000);
  const suffix = diff >= 0 ? "from now" : "ago";
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min ${suffix}`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} hr ${suffix}`;
  const days = Math.round(hours / 24);
  return `${days} day${days === 1 ? "" : "s"} ${suffix}`;
}

export function initials(name?: string | null) {
  if (!name) return "?";
  return name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase())
    .join("");
}

export function scoreTone(score?: number | null) {
  if (score === null || score === undefined) return "slate";
  if (score >= 75) return "green";
  if (score >= 55) return "amber";
  return "rose";
}

export function pct(value?: number | null) {
  if (value === null || value === undefined) return 0;
  return Math.max(0, Math.min(100, Math.round(value)));
}

export function humanize(value?: string | null) {
  if (!value) return "—";
  return value
    .toLowerCase()
    .split("_")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

export function providerLabel(provider?: string | null) {
  switch (provider) {
    case "microsoft_teams":
    case "teams":
      return "Microsoft Teams";
    case "google_meet":
      return "Google Meet";
    case "google_calendar":
    case "googlecalendar":
      return "Google Calendar & Meet";
    case "gmail":
      return "Gmail";
    case "none":
      return "No video link";
    default:
      return provider ? humanize(provider) : "—";
  }
}

export function statusTone(status?: string | null): string {
  switch ((status || "").toUpperCase()) {
    case "SELECTED":
    case "SUCCEEDED":
    case "SENT":
    case "ACTIVE":
    case "SCHEDULED":
    case "PARSED":
      return "green";
    case "SHORTLISTED":
    case "INTERVIEW_SCHEDULED":
    case "PARTIAL":
    case "PENDING":
    case "PENDING_APPROVAL":
    case "RETRYING":
      return "amber";
    case "REJECTED":
    case "FAILED":
    case "ERROR":
    case "REVOKED":
    case "CANCELLED":
      return "rose";
    case "INTERVIEWED":
    case "CONTACTED":
      return "indigo";
    default:
      return "slate";
  }
}
