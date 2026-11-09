"use client";

import Link from "next/link";
import { useState } from "react";
import { Calendar, CheckCircle2, Loader2, Mail, Plug, RefreshCw, Trash2, Video } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { formatDateTime, providerLabel } from "@/lib/format";
import type { Connector } from "@/lib/types";
import { useWorkspace } from "@/app/providers";
import { Alert, Card, PageHeader, StatusBadge } from "@/components/ui";

const PROVIDERS = [
  {
    key: "gmail",
    label: "Gmail",
    description: "Imports inbound applications and sends candidate email.",
    icon: Mail,
  },
  {
    key: "google_calendar",
    label: "Google Calendar & Meet",
    description:
      "Creates the calendar event and its Google Meet link. Use a Google Workspace account for guaranteed Meet links.",
    icon: Calendar,
  },
  {
    key: "google_meet",
    label: "Google Meet (standalone)",
    description:
      "Creates a Meet room without a calendar event. Use this when you cannot attach Meet links to Google Calendar.",
    icon: Video,
  },
  {
    key: "microsoft_teams",
    label: "Microsoft Teams",
    description: "Creates real Teams online meetings for Microsoft 365 Work/School organizations.",
    icon: Video,
  },
] as const;

export default function IntegrationsSettings() {
  const { me, refresh: reloadMe } = useWorkspace();
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [oauthUrl, setOauthUrl] = useState<string | null>(null);

  const connectors: Connector[] = me?.connectors || [];
  const byProvider = (provider: string) => connectors.find((item) => item.provider === provider);

  async function connect(provider: string) {
    setBusy(`connect:${provider}`);
    setError(null);
    setNotice(null);
    setOauthUrl(null);
    // Open the tab while the click is still the active gesture. Calling
    // window.open after awaiting the API is treated as a popup and blocked.
    const tab = window.open("about:blank", "_blank");
    try {
      const result = await api<{ status: string; redirect_url?: string | null; message?: string }>(
        "/api/connectors/connect",
        { method: "POST", body: { provider } },
      );
      if (!result.redirect_url) {
        tab?.close();
        setError("Could not start the authorization. Try again.");
        return;
      }
      setOauthUrl(result.redirect_url);
      if (tab) {
        tab.opener = null;
        tab.location.replace(result.redirect_url);
      } else {
        window.open(result.redirect_url, "_blank", "noopener,noreferrer");
      }
      setNotice(result.message || "Connection started. Complete the OAuth flow, then verify.");
      await reloadMe();
    } catch (err) {
      tab?.close();
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  }

  async function verify(connector: Connector) {
    setBusy(`verify:${connector.id}`);
    setError(null);
    setNotice(null);
    try {
      const result = await api<{ status: string; connected_email?: string | null }>(
        `/api/connectors/${connector.id}/verify`,
        { method: "POST" },
      );
      setNotice(
        result.status === "active"
          ? `Connected${result.connected_email ? ` as ${result.connected_email}` : ""}.`
          : `Connection state: ${result.status}`,
      );
      await reloadMe();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  }

  async function disconnect(connector: Connector) {
    const confirmed =
      typeof window === "undefined" ||
      window.confirm(
        `Disconnect ${providerLabel(connector.provider)}? Inbox sync will stop and scheduling will fail until you reconnect.`,
      );
    if (!confirmed) return;
    setBusy(`disconnect:${connector.id}`);
    setError(null);
    setNotice(null);
    try {
      await api(`/api/connectors/${connector.id}`, { method: "DELETE" });
      setNotice(`${providerLabel(connector.provider)} disconnected.`);
      await reloadMe();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="space-y-5">
      <PageHeader
        title="Integrations"
        subtitle="Connect the email and meeting accounts used for applications, outreach and interviews."
        actions={
          <>
            <Link href="/settings/sync" className="btn-secondary btn-sm">
              Inbox sync
            </Link>
            <Link href="/settings/team" className="btn-secondary btn-sm">
              Team
            </Link>
          </>
        }
      />

      {error ? <Alert tone="error">{error}</Alert> : null}
      {notice ? (
        <Alert tone="success">
          {notice}
          {oauthUrl ? (
            <>
              {" "}
              <a className="underline" href={oauthUrl} target="_blank" rel="noreferrer">
                Open the authorization page
              </a>
            </>
          ) : null}
        </Alert>
      ) : null}

      <div className="grid gap-4 md:grid-cols-2">
        {PROVIDERS.map((provider) => {
          const connector = byProvider(provider.key);
          const Icon = provider.icon;
          const active = connector?.status === "active";
          return (
            <Card key={provider.key} className="flex flex-col gap-3 p-5">
              <div className="flex items-start gap-3">
                <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-[7px] border border-[#ececea] bg-[#fafaf9] text-[#71717a]">
                  <Icon size={18} />
                </span>
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <h2 className="text-[13px] font-medium text-[#18181b]">{provider.label}</h2>
                    {connector ? <StatusBadge status={connector.status} /> : <span className="badge-slate">not connected</span>}
                  </div>
                  <p className="mt-1 text-[11.5px] leading-5 text-[#8a8a93]">{provider.description}</p>
                </div>
              </div>

              {connector ? (
                <dl className="grid grid-cols-1 gap-2 rounded-[8px] border border-[#f0f0ed] bg-[#fcfcfb] p-3 text-[11px] text-[#52525b]">
                  <div>
                    <dt className="text-[#a1a1aa]">Account</dt>
                    <dd className="truncate">{connector.connected_email || connector.display_name || "—"}</dd>
                  </div>
                  {connector.last_error ? (
                    <div>
                      <dt className="text-[#a1a1aa]">Last error</dt>
                      <dd className="text-[#a33a2c]">{connector.last_error}</dd>
                    </div>
                  ) : null}
                </dl>
              ) : null}

              <div className="mt-auto flex flex-wrap gap-2">
                {!active ? (
                  <button
                    className="btn-primary btn-sm"
                    onClick={() => void connect(provider.key)}
                    disabled={busy !== null}
                  >
                    {busy === `connect:${provider.key}` ? (
                      <Loader2 size={13} className="animate-spin" />
                    ) : (
                      <Plug size={13} />
                    )}
                    Connect {provider.label}
                  </button>
                ) : (
                  <span className="badge-green">
                    <CheckCircle2 size={12} /> Ready
                  </span>
                )}
                {connector ? (
                  <button
                    className="btn-secondary btn-sm"
                    onClick={() => void verify(connector)}
                    disabled={busy !== null}
                  >
                    {busy === `verify:${connector.id}` ? (
                      <Loader2 size={13} className="animate-spin" />
                    ) : (
                      <RefreshCw size={13} />
                    )}
                    Verify
                  </button>
                ) : null}
                {connector && connector.status !== "revoked" ? (
                  <button
                    className="btn-danger btn-sm"
                    onClick={() => void disconnect(connector)}
                    disabled={busy !== null}
                  >
                    {busy === `disconnect:${connector.id}` ? (
                      <Loader2 size={13} className="animate-spin" />
                    ) : (
                      <Trash2 size={13} />
                    )}
                    Disconnect
                  </button>
                ) : null}
              </div>
            </Card>
          );
        })}
      </div>

      <Card className="p-5">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h2 className="text-[13px] font-medium text-[#18181b]">Meeting provider</h2>
            <p className="mt-0.5 text-[11.5px] text-[#8a8a93]">
              There is no workspace default. You choose Google Meet or Microsoft Teams for each
              interview in chat, and the real link is created before the invitation is sent.
            </p>
          </div>
        </div>
      </Card>

      <div className="flex flex-wrap items-center gap-2 text-[10.5px] text-[#a1a1aa]">
        OAuth links open in a new tab. Return here and press Verify.
        {connectors[0]?.last_error ? (
          <span className="ml-2 text-[#a33a2c]">Last error: {connectors[0].last_error}</span>
        ) : null}
        {connectors.length === 0 ? <span>No connectors yet.</span> : null}
        {connectors[0]?.created_at ? <span>Connected since {formatDateTime(connectors[0].created_at)}</span> : null}
      </div>
    </div>
  );
}
