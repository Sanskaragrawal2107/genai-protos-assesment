import { createClient } from "@supabase/supabase-js";

// Fallback to configured Supabase project (sncotroyqgbputmwqiou) when building in environments
// where .env.local is not present (e.g. Netlify build step prerendering).
const DEFAULT_SUPABASE_URL = "https://sncotroyqgbputmwqiou.supabase.co";
const DEFAULT_SUPABASE_ANON_KEY =
  "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InNuY290cm95cWdicHV0bXdxaW91Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3OTM5NjIzMTYsImV4cCI6MjEwOTUzODMxNn0.BwZpZzSG7uyGaUMQI7RQm2yBCvDA1H_k1n4PNVsAsCg";

const url = process.env.NEXT_PUBLIC_SUPABASE_URL || DEFAULT_SUPABASE_URL;
const anonKey = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY || DEFAULT_SUPABASE_ANON_KEY;

export const supabase = createClient(url, anonKey, {
  auth: { persistSession: true, autoRefreshToken: true, detectSessionInUrl: true },
});

