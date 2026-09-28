import { createClient } from "@supabase/supabase-js";

const url = (import.meta.env.VITE_SUPABASE_URL ?? "").trim();
const anonKey = (import.meta.env.VITE_SUPABASE_ANON_KEY ?? "").trim();

/** The anon key is public by design. Never put the Supabase service-role key here. */
export const supabase = url && anonKey
  ? createClient(url, anonKey, {
      auth: {
        storage: typeof window === "undefined" ? undefined : window.sessionStorage,
        persistSession: true,
        autoRefreshToken: true,
        detectSessionInUrl: true,
      },
    })
  : null;

export const supabaseAuthConfigured = Boolean(supabase);
