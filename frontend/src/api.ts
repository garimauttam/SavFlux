import { supabase } from "./lib/supabase";

const API_BASE = import.meta.env.VITE_API_URL ?? "";

export function apiUrl(path: string): string {
  return `${API_BASE}${path}`;
}

export async function apiFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers);
  if (supabase) {
    try {
      const { data } = await supabase.auth.getSession();
      const accessToken = data.session?.access_token;
      if (accessToken) headers.set("Authorization", `Bearer ${accessToken}`);
    } catch {
      // The API will return a clear sign-in error if the session cannot be read.
    }
  }
  return fetch(apiUrl(path), { ...init, headers });
}
