"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { api, setAccessToken } from "@/lib/api";
import { supabase } from "@/lib/supabase";
import type { Me } from "@/lib/types";

interface AuthState {
  ready: boolean;
  token: string | null;
  email: string | null;
  userId: string | null;
  signOut: () => Promise<void>;
}

interface WorkspaceState {
  me: Me | null;
  loading: boolean;
  error: string | null;
  refresh: () => Promise<void>;
}

const AuthContext = createContext<AuthState | null>(null);
const WorkspaceContext = createContext<WorkspaceState | null>(null);

export function Providers({ children }: { children: React.ReactNode }) {
  const [ready, setReady] = useState(false);
  const [token, setToken] = useState<string | null>(null);
  const [email, setEmail] = useState<string | null>(null);
  const [userId, setUserId] = useState<string | null>(null);
  const [me, setMe] = useState<Me | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let mounted = true;

    supabase.auth.getSession().then(({ data }) => {
      if (!mounted) return;
      const session = data.session;
      setToken(session?.access_token ?? null);
      setEmail(session?.user?.email ?? null);
      setUserId(session?.user?.id ?? null);
      setAccessToken(session?.access_token ?? null);
      setReady(true);
    });

    const { data: listener } = supabase.auth.onAuthStateChange((_event, session) => {
      setToken(session?.access_token ?? null);
      setEmail(session?.user?.email ?? null);
      setUserId(session?.user?.id ?? null);
      setAccessToken(session?.access_token ?? null);
    });

    return () => {
      mounted = false;
      listener.subscription.unsubscribe();
    };
  }, []);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await api<Me>("/api/me");
      setMe(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load workspace");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!ready) return;
    if (!token) {
      setLoading(false);
      return;
    }
    void refresh();
  }, [ready, token, refresh]);

  const signOut = useCallback(async () => {
    await supabase.auth.signOut();
    setAccessToken(null);
    setToken(null);
    setEmail(null);
    setUserId(null);
    setMe(null);
  }, []);

  const authValue = useMemo<AuthState>(
    () => ({
      ready,
      token,
      email,
      userId,
      signOut,
    }),
    [ready, token, email, userId, signOut],
  );

  const workspaceValue = useMemo<WorkspaceState>(
    () => ({ me, loading, error, refresh }),
    [me, loading, error, refresh],
  );

  return (
    <AuthContext.Provider value={authValue}>
      <WorkspaceContext.Provider value={workspaceValue}>{children}</WorkspaceContext.Provider>
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside Providers");
  return ctx;
}

export function useWorkspace() {
  const ctx = useContext(WorkspaceContext);
  if (!ctx) throw new Error("useWorkspace must be used inside Providers");
  return ctx;
}
