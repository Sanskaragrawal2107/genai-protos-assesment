"use client";

import Link from "next/link";
import { Users } from "lucide-react";
import { formatDate } from "@/lib/format";
import { useFetch } from "@/lib/useFetch";
import { useWorkspace } from "@/app/providers";
import { Alert, Avatar, Card, CardHeader, EmptyState, PageHeader, Spinner, StatusBadge } from "@/components/ui";

interface TeamMember {
  id: string;
  user_id: string;
  email?: string | null;
  display_name?: string | null;
  role: string;
  is_you: boolean;
  created_at?: string | null;
}

export default function TeamSettingsPage() {
  const { me } = useWorkspace();
  const team = useFetch<TeamMember[]>("/api/team");

  return (
    <div className="space-y-5">
      <PageHeader
        title="Workspace & team"
        subtitle="The people who can access this workspace."
        actions={
          <Link href="/settings/integrations" className="btn-secondary btn-sm">
            Integrations
          </Link>
        }
      />

      <div className="grid gap-4 md:grid-cols-2">
        <Card className="p-4">
          <p className="stat-label">Workspace</p>
          <p className="mt-1 truncate text-[13px] font-medium text-[#18181b]">{me?.workspace.name || "—"}</p>
        </Card>
        <Card className="p-4">
          <p className="stat-label">Meeting provider</p>
          <p className="mt-1 text-[13px] font-medium text-[#18181b]">Chosen per interview</p>
          <p className="mt-1 text-[11px] text-[#a1a1aa]">
            Pick Google Meet or Microsoft Teams in chat · Email channel: Gmail only
          </p>
        </Card>
      </div>

      {team.error ? (
        <Alert tone="error" title="Could not load the team">
          {team.error.message}
        </Alert>
      ) : null}

      <Card>
        <CardHeader
          title="Members"
          subtitle={`${team.data?.length ?? 0} people in this workspace`}
          action={<Users size={15} className="text-[#a1a1aa]" />}
        />
        {team.loading ? (
          <div className="p-6">
            <Spinner label="Loading team…" />
          </div>
        ) : (team.data || []).length === 0 ? (
          <EmptyState icon={<Users size={20} />} title="No members" description="No teammates have joined this workspace yet." />
        ) : (
          <ul className="divide-y divide-[#f4f4f1]">
            {(team.data || []).map((member) => (
              <li key={member.id} className="flex items-center gap-3 px-5 py-3">
                <Avatar name={member.display_name || member.email || member.user_id} size={34} />
                <div className="min-w-0 flex-1">
                  <p className="truncate text-[12.5px] font-medium text-[#18181b]">
                    {member.display_name || member.email || member.user_id}
                    {member.is_you ? <span className="ml-2 badge-indigo">you</span> : null}
                  </p>
                  <p className="truncate text-[11px] text-[#8a8a93]">{member.email || member.user_id}</p>
                </div>
                <StatusBadge status={member.role} />
                <span className="hidden text-[10.5px] text-[#a1a1aa] sm:block">
                  joined {formatDate(member.created_at)}
                </span>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}
