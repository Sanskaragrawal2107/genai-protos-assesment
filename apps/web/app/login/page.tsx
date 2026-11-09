"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { ArrowRight, Loader2, Lock, Mail } from "lucide-react";
import { useAuth } from "@/app/providers";
import { supabase } from "@/lib/supabase";
import { Alert } from "@/components/ui";

export default function LoginPage() {
  const router = useRouter();
  const { token } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Once Supabase establishes a session there is nothing left to do here.
  useEffect(() => {
    if (token) router.replace("/");
  }, [token, router]);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const { error } = await supabase.auth.signInWithPassword({ email, password });
      if (error) throw error;
      router.replace("/");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex min-h-screen bg-white">
      <div className="flex flex-1 items-center justify-center px-5 py-12">
        <div className="w-full max-w-[368px]">
          <div className="mb-7">
            <div className="flex items-center gap-2.5">
              <span className="flex h-8 w-8 items-center justify-center rounded-[7px] bg-[#1f4b3f] text-[12px] font-semibold text-white">
                R
              </span>
              <div className="leading-tight">
                <p className="text-[13px] font-semibold tracking-[-0.01em] text-[#18181b]">
                  Recruiter OS
                </p>
                <p className="text-[10.5px] text-[#a1a1aa]">Recruitment intelligence</p>
              </div>
            </div>
          </div>

          <h1 className="text-[24px] font-semibold leading-8 tracking-[-0.025em] text-[#18181b]">
            Sign in to your workspace
          </h1>
          <p className="mt-1.5 text-[12.5px] leading-5 text-[#71717a]">
            Use your work email and password. Access is limited to invited members of this
            workspace.
          </p>

          <div className="mt-7">
            <form className="space-y-4" onSubmit={submit}>
              <div>
                <label className="label">Work email</label>
                <div className="relative">
                  <Mail
                    size={14}
                    className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-[#a1a1aa]"
                  />
                  <input
                    className="input pl-9"
                    type="email"
                    autoComplete="email"
                    placeholder="you@company.com"
                    value={email}
                    onChange={(event) => setEmail(event.target.value)}
                    required
                  />
                </div>
              </div>

              <div>
                <label className="label">Password</label>
                <div className="relative">
                  <Lock
                    size={14}
                    className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-[#a1a1aa]"
                  />
                  <input
                    className="input pl-9"
                    type="password"
                    autoComplete="current-password"
                    placeholder="••••••••"
                    value={password}
                    onChange={(event) => setPassword(event.target.value)}
                    required
                  />
                </div>
              </div>

              {error ? <Alert tone="error">{error}</Alert> : null}

              <button className="btn-primary w-full" type="submit" disabled={busy}>
                {busy ? <Loader2 size={14} className="animate-spin" /> : <ArrowRight size={14} />}
                Sign in
              </button>
            </form>
          </div>

          <div className="mt-10 border-t border-[#f0f0ed] pt-4">
            <p className="text-[10.5px] leading-4 text-[#a1a1aa]">
              Access is limited to invited members of this workspace.
            </p>
          </div>
        </div>
      </div>

      <div className="hidden w-[44%] border-l border-[#ececea] bg-[#fcfcfb] lg:flex lg:flex-col lg:justify-between lg:p-10">
        <p className="text-[10px] font-semibold uppercase tracking-[0.1em] text-[#a1a1aa]">
          Recruitment intelligence
        </p>
        <div className="max-w-[360px]">
          <p className="text-[20px] font-medium leading-8 tracking-[-0.015em] text-[#27272a]">
            One pipeline from application to interview, without losing track of anyone.
          </p>
          <ul className="mt-6 space-y-3.5">
            {[
              ["Inbox to pipeline", "Resumes arrive from Gmail and are structured automatically."],
              ["Ranked shortlists", "Compare candidates by fit, experience and skills."],
              ["Approval-gated actions", "Nothing is emailed or booked without a recruiter."],
            ].map(([title, body]) => (
              <li key={title} className="flex gap-3">
                <span className="mt-[7px] h-1 w-1 shrink-0 rounded-full bg-[#1f4b3f]" />
                <div>
                  <p className="text-[12.5px] font-medium text-[#27272a]">{title}</p>
                  <p className="mt-0.5 text-[11.5px] leading-5 text-[#8a8a93]">{body}</p>
                </div>
              </li>
            ))}
          </ul>
        </div>
        <p className="text-[10.5px] text-[#b4b4ba]">© {new Date().getFullYear()} Recruiter OS</p>
      </div>
    </div>
  );
}
