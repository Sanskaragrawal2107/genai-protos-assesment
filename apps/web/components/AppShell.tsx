"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import {
  Briefcase,
  CalendarDays,
  ChevronsUpDown,
  LayoutDashboard,
  LogOut,
  Mail,
  Menu,
  MessageSquare,
  PanelLeftClose,
  PanelLeftOpen,
  Plug,
  RefreshCw,
  Star,
  Users,
  X,
} from "lucide-react";
import { useAuth, useWorkspace } from "@/app/providers";
import { Avatar } from "@/components/ui";
import { initials } from "@/lib/format";

const MAIN_NAV = [
  { href: "/", label: "Overview", icon: LayoutDashboard },
  { href: "/candidates", label: "Candidates", icon: Users },
  { href: "/shortlists", label: "Shortlists", icon: Star },
  { href: "/jobs", label: "Job openings", icon: Briefcase },
  { href: "/interviews", label: "Interviews", icon: CalendarDays },
  { href: "/communications", label: "Inbox & outreach", icon: Mail },
];

const INTELLIGENCE_NAV = [{ href: "/assistant", label: "Assistant", icon: MessageSquare }];

const SETTINGS_NAV = [
  { href: "/settings/integrations", label: "Integrations", icon: Plug },
  { href: "/settings/sync", label: "Inbox sync", icon: RefreshCw },
  { href: "/settings/team", label: "Workspace", icon: Users },
];

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [collapsed, setCollapsed] = useState(false);
  const { me, loading } = useWorkspace();
  const { ready, email, token, signOut } = useAuth();

  useEffect(() => {
    setOpen(false);
  }, [pathname]);

  useEffect(() => {
    setCollapsed(window.localStorage.getItem("recruiter.sidebar.collapsed") === "1");
  }, []);

  function toggleCollapsed() {
    setCollapsed((value) => {
      const next = !value;
      window.localStorage.setItem("recruiter.sidebar.collapsed", next ? "1" : "0");
      return next;
    });
  }

  useEffect(() => {
    if (ready && !token && pathname !== "/login") router.replace("/login");
  }, [ready, token, pathname, router]);

  if (pathname === "/login") {
    return <>{children}</>;
  }

  if (!ready || !token) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-white text-[12.5px] font-medium text-[#8a8a93]">
        <div className="flex items-center gap-2.5">
          <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-[#e4e4e7] border-t-[#1f4b3f]" />
          <span>Loading workspace…</span>
        </div>
      </div>
    );
  }

  const workspaceName = loading ? "Workspace" : me?.workspace.name || "Workspace";
  const isAssistant = pathname.startsWith("/assistant");

  return (
    <div
      className={`flex bg-white ${
        isAssistant ? "h-dvh overflow-hidden" : "min-h-screen"
      }`}
    >
      <aside
        className={`fixed inset-y-0 left-0 z-40 flex w-[248px] flex-col border-r border-[#ececea] bg-[#fcfcfb] transition-[width,transform] duration-200 lg:sticky lg:top-0 lg:h-screen lg:translate-x-0 ${
          open ? "translate-x-0" : "-translate-x-full"
        } ${collapsed ? "lg:w-[64px]" : "lg:w-[248px]"}`}
      >
        <div
          className={`flex h-14 items-center px-3 ${
            collapsed ? "justify-between lg:justify-center lg:px-0" : "justify-between"
          }`}
        >
          <Link href="/" className={`flex min-w-0 items-center gap-2.5 ${collapsed ? "lg:hidden" : ""}`}>
            <span className="flex h-7 w-7 items-center justify-center rounded-[7px] bg-[#1f4b3f] text-[11px] font-bold tracking-tight text-white">
              R
            </span>
            <span className="truncate text-[13.5px] font-semibold tracking-[-0.015em] text-[#18181b]">
              Recruiter OS
            </span>
          </Link>
          <button
            className="hidden rounded-[6px] p-1.5 text-[#a1a1aa] transition-colors hover:bg-[#f0f0ed] hover:text-[#52525b] lg:inline-flex"
            onClick={toggleCollapsed}
            aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
            title={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          >
            {collapsed ? <PanelLeftOpen size={16} /> : <PanelLeftClose size={16} />}
          </button>
          <button
            className="rounded-[6px] p-1 text-[#a1a1aa] hover:bg-[#f0f0ed] lg:hidden"
            onClick={() => setOpen(false)}
            aria-label="Close navigation"
          >
            <X size={17} />
          </button>
        </div>

        <div className={`px-3 pb-3 ${collapsed ? "lg:px-2" : ""}`}>
          <button
            type="button"
            title={workspaceName}
            className={`flex w-full items-center gap-2.5 rounded-[7px] border border-[#ececea] bg-white px-2.5 py-2 text-left transition-colors hover:border-[#dcdcd8] ${
              collapsed ? "lg:justify-center lg:px-0" : ""
            }`}
          >
            <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-[5px] bg-[#f2f2ef] text-[10px] font-semibold text-[#5c5c66]">
              {initials(workspaceName)}
            </span>
            <span className={`min-w-0 flex-1 ${collapsed ? "lg:hidden" : ""}`}>
              <span className="block truncate text-[12px] font-medium text-[#27272a]">
                {workspaceName}
              </span>
              <span className="block text-[10.5px] leading-3 text-[#a1a1aa]">Workspace</span>
            </span>
            <ChevronsUpDown size={13} className={`shrink-0 text-[#a1a1aa] ${collapsed ? "lg:hidden" : ""}`} />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto px-3 pb-4">
          <p className={`eyebrow px-2 pb-1.5 pt-2 ${collapsed ? "lg:hidden" : ""}`}>Workspace</p>
          <nav className="space-y-px">
            {MAIN_NAV.map((item) => {
              const active = item.href === "/" ? pathname === "/" : pathname.startsWith(item.href);
              const Icon = item.icon;
              return (
                <Link
                  key={item.href}
                  href={item.href}
                  title={item.label}
                  className={`sidebar-link ${active ? "sidebar-link-active" : ""} ${
                    collapsed ? "lg:justify-center lg:px-0" : ""
                  }`}
                >
                  <Icon
                    size={15.5}
                    className={active ? "text-[#1f4b3f]" : "text-[#a1a1aa]"}
                    strokeWidth={1.8}
                  />
                  <span className={`truncate ${collapsed ? "lg:hidden" : ""}`}>{item.label}</span>
                </Link>
              );
            })}
          </nav>

          <p className={`eyebrow px-2 pb-1.5 pt-5 ${collapsed ? "lg:hidden" : ""}`}>Intelligence</p>
          <nav className="space-y-px">
            {INTELLIGENCE_NAV.map((item) => {
              const active = pathname.startsWith(item.href);
              const Icon = item.icon;
              return (
                <Link
                  key={item.href}
                  href={item.href}
                  title={item.label}
                  className={`sidebar-link ${active ? "sidebar-link-active" : ""} ${
                    collapsed ? "lg:justify-center lg:px-0" : ""
                  }`}
                >
                  <Icon
                    size={15.5}
                    className={active ? "text-[#1f4b3f]" : "text-[#a1a1aa]"}
                    strokeWidth={1.8}
                  />
                  <span className={`truncate ${collapsed ? "lg:hidden" : ""}`}>{item.label}</span>
                </Link>
              );
            })}
          </nav>

          <p className={`eyebrow px-2 pb-1.5 pt-5 ${collapsed ? "lg:hidden" : ""}`}>Settings</p>
          <nav className="space-y-px">
            {SETTINGS_NAV.map((item) => {
              const active = pathname === item.href;
              const Icon = item.icon;
              return (
                <Link
                  key={item.href}
                  href={item.href}
                  title={item.label}
                  className={`sidebar-link ${active ? "sidebar-link-active" : ""} ${
                    collapsed ? "lg:justify-center lg:px-0" : ""
                  }`}
                >
                  <Icon
                    size={15.5}
                    className={active ? "text-[#1f4b3f]" : "text-[#a1a1aa]"}
                    strokeWidth={1.8}
                  />
                  <span className={`truncate ${collapsed ? "lg:hidden" : ""}`}>{item.label}</span>
                </Link>
              );
            })}
          </nav>
        </div>

        <div className={`border-t border-[#ececea] p-3 ${collapsed ? "lg:px-2" : ""}`}>
          <div className={`flex items-center gap-2.5 ${collapsed ? "lg:flex-col lg:gap-1.5" : ""}`}>
            <Avatar name={me?.identity.name || email || "R"} size={28} />
            <div className={`min-w-0 flex-1 ${collapsed ? "lg:hidden" : ""}`}>
              <p className="truncate text-[12px] font-medium text-[#27272a]">
                {me?.identity.name || email?.split("@")[0] || "Recruiter"}
              </p>
              <p className="truncate text-[10.5px] leading-3 text-[#a1a1aa]">
                {email || "Signed in"}
              </p>
            </div>
            <button
              onClick={() => signOut()}
              title="Sign out"
              className="rounded-[6px] p-1.5 text-[#a1a1aa] transition-colors hover:bg-[#f0f0ed] hover:text-[#52525b]"
            >
              <LogOut size={14} />
            </button>
          </div>
        </div>
      </aside>

      {open && (
        <div
          className="fixed inset-0 z-30 bg-[#18181b]/20 backdrop-blur-[2px] lg:hidden"
          onClick={() => setOpen(false)}
        />
      )}

      <div className="flex min-w-0 flex-1 flex-col">
        <header
          className={`sticky top-0 z-20 flex h-14 items-center justify-between border-b border-[#ececea] bg-white/95 px-4 backdrop-blur-md lg:px-8 ${
            isAssistant ? "lg:hidden" : ""
          }`}
        >
          <div className="flex min-w-0 items-center gap-3">
            <button
              className="rounded-[6px] p-1.5 text-[#71717a] hover:bg-[#f4f4f1] lg:hidden"
              onClick={() => setOpen(true)}
              aria-label="Open navigation"
            >
              <Menu size={18} />
            </button>
            <nav className="flex min-w-0 items-center gap-1.5 text-[12px]">
              <span className="hidden max-w-[180px] truncate text-[#a1a1aa] sm:inline">
                {workspaceName}
              </span>
              <span className="hidden text-[#d4d4d8] sm:inline">/</span>
              <span className="truncate font-medium text-[#27272a]">{pageTitle(pathname)}</span>
            </nav>
          </div>

          <div className="flex items-center gap-1">
            <Link href="/assistant" className="topbar-link">
              <MessageSquare size={14} strokeWidth={1.8} />
              <span className="hidden sm:inline">Ask assistant</span>
            </Link>
            <span className="mx-1 h-4 w-px bg-[#ececea]" />
            <Link
              href="/settings/team"
              className="rounded-full transition-opacity hover:opacity-80"
              aria-label="Account"
            >
              <Avatar name={me?.identity.name || email || "R"} size={26} />
            </Link>
          </div>
        </header>

        <main
          className={
            isAssistant
              ? "flex min-h-0 flex-1 flex-col"
              : "mx-auto w-full max-w-[1480px] flex-1 px-5 py-7 lg:px-9 lg:py-9"
          }
        >
          {pathname.startsWith("/settings") ? <SettingsNav pathname={pathname} /> : null}
          {children}
        </main>
      </div>
    </div>
  );
}

function SettingsNav({ pathname }: { pathname: string }) {
  return (
    <nav className="mb-7 flex flex-wrap gap-1 border-b border-[#ececea]">
      {SETTINGS_NAV.map((item) => {
        const active = pathname === item.href;
        return (
          <Link
            key={item.href}
            href={item.href}
            className={`-mb-px border-b-2 px-3 py-2 text-[12.5px] font-medium transition-colors ${
              active
                ? "border-[#1f4b3f] text-[#18181b]"
                : "border-transparent text-[#8a8a93] hover:text-[#3f3f46]"
            }`}
          >
            {item.label}
          </Link>
        );
      })}
    </nav>
  );
}

function pageTitle(pathname: string) {
  if (pathname === "/") return "Overview";
  if (pathname.startsWith("/assistant")) return "Assistant";
  if (pathname.startsWith("/candidates/")) return "Candidate";
  if (pathname.startsWith("/candidates")) return "Candidates";
  if (pathname.startsWith("/jobs/")) return "Job";
  if (pathname.startsWith("/jobs")) return "Job openings";
  if (pathname.startsWith("/shortlists")) return "Shortlists";
  if (pathname.startsWith("/interviews")) return "Interviews";
  if (pathname.startsWith("/communications")) return "Inbox & outreach";
  if (pathname.startsWith("/settings")) return "Settings";
  return "Recruiter OS";
}