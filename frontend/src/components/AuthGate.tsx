import { useCallback, useEffect, useState, type FormEvent, type ReactNode } from "react";
import { ArrowLeft, ArrowRight, Loader2, LockKeyhole, Mail, ShieldCheck } from "lucide-react";
import { apiUrl } from "../api";
import { supabase, supabaseAuthConfigured } from "../lib/supabase";

type GateState = "checking" | "signed-out" | "signed-in" | "unavailable";
type AuthMode = "signin" | "signup" | "reset" | "update-password";

export function AuthGate({ children }: { children: ReactNode }) {
  const [state, setState] = useState<GateState>(supabaseAuthConfigured ? "checking" : "unavailable");
  const [mode, setMode] = useState<AuthMode>("signin");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const verifyBackendSession = useCallback(async (accessToken: string) => {
    try {
      const response = await fetch(apiUrl("/api/v1/auth/session"), {
        headers: { Authorization: `Bearer ${accessToken}` },
      });
      if (response.ok) {
        setState("signed-in");
        setError("");
        return;
      }
      const payload = await response.json().catch(() => ({}));
      setState("unavailable");
      setError(typeof payload?.detail === "string" ? payload.detail : `SavFlux could not verify your account (HTTP ${response.status}).`);
    } catch {
      setState("unavailable");
      setError("Could not reach the SavFlux server. Check the connection and retry.");
    }
  }, []);

  const check = useCallback(async () => {
    if (!supabase) {
      setState("unavailable");
      setError("");
      return;
    }
    setState("checking");
    const { data, error: sessionError } = await supabase.auth.getSession();
    if (sessionError) {
      setState("signed-out");
      setError("Your saved sign-in could not be read. Please sign in again.");
      return;
    }
    if (!data.session) {
      setState("signed-out");
      return;
    }
    await verifyBackendSession(data.session.access_token);
  }, [verifyBackendSession]);

  useEffect(() => {
    if (!supabase) return;
    void check();
    const { data: { subscription } } = supabase.auth.onAuthStateChange((event, session) => {
      setError("");
      setNotice("");
      if (event === "PASSWORD_RECOVERY") {
        setMode("update-password");
        setState("signed-out");
        return;
      }
      if (session?.access_token) {
        setState("checking");
        void verifyBackendSession(session.access_token);
      } else {
        setState("signed-out");
      }
    });
    const onSignOut = () => {
      void supabase?.auth.signOut();
      setPassword("");
      setError("");
      setNotice("");
      setMode("signin");
      setState("signed-out");
    };
    window.addEventListener("savflux:signout", onSignOut);
    return () => {
      subscription.unsubscribe();
      window.removeEventListener("savflux:signout", onSignOut);
    };
  }, [check, verifyBackendSession]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!supabase || busy) return;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      if (mode === "reset") {
        const { error: resetError } = await supabase.auth.resetPasswordForEmail(email.trim(), {
          redirectTo: window.location.origin,
        });
        if (resetError) throw resetError;
        setNotice("If an account exists for that email, Supabase will send a password-reset link.");
      } else if (mode === "update-password") {
        const { error: updateError } = await supabase.auth.updateUser({ password });
        if (updateError) throw updateError;
        setNotice("Your password has been updated.");
        const { data } = await supabase.auth.getSession();
        if (data.session?.access_token) await verifyBackendSession(data.session.access_token);
      } else if (mode === "signup") {
        const { data, error: signupError } = await supabase.auth.signUp({
          email: email.trim(),
          password,
          options: { emailRedirectTo: window.location.origin },
        });
        if (signupError) throw signupError;
        if (data.session?.access_token) await verifyBackendSession(data.session.access_token);
        else setNotice("Check your email to verify your account, then sign in.");
      } else {
        const { data, error: signInError } = await supabase.auth.signInWithPassword({
          email: email.trim(),
          password,
        });
        if (signInError) throw signInError;
        if (data.session?.access_token) await verifyBackendSession(data.session.access_token);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Sign-in failed. Please try again.");
    } finally {
      setBusy(false);
    }
  }

  async function signInWithGoogle() {
    if (!supabase || busy) return;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const { error: oauthError } = await supabase.auth.signInWithOAuth({
        provider: "google",
        options: { redirectTo: window.location.origin },
      });
      if (oauthError) setError(oauthError.message);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Google sign-in could not be started. Please try again.");
    } finally {
      setBusy(false);
    }
  }

  if (state === "signed-in") return <>{children}</>;

  return (
    <main className="flex min-h-full items-center justify-center bg-[var(--sf-canvas)] px-5 py-12 text-[var(--sf-text)]">
      <section className="w-full max-w-[440px] rounded-2xl border border-[var(--sf-line)] bg-[var(--sf-surface)] p-7 shadow-2xl shadow-black/20 sm:p-9">
        <div className="mb-8 flex items-center gap-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-[var(--sf-accent-soft)] text-[var(--sf-accent)]"><ShieldCheck className="h-5 w-5" /></div>
          <div><p className="text-[15px] font-semibold tracking-tight">SavFlux</p><p className="text-[12px] text-[var(--sf-text-mute)]">Your private code workspace</p></div>
        </div>

        {state === "checking" ? (
          <div role="status" className="flex items-center justify-center gap-2 py-12 text-sm text-[var(--sf-text-dim)]"><Loader2 className="h-4 w-4 animate-spin" /> Checking your account…</div>
        ) : !supabaseAuthConfigured ? (
          <div>
            <div className="mb-6 flex h-10 w-10 items-center justify-center rounded-xl border border-[var(--sf-line)] bg-[var(--sf-raised)] text-[var(--sf-text-dim)]"><LockKeyhole className="h-4 w-4" /></div>
            <h1 className="text-xl font-semibold tracking-tight">Sign-in is not configured</h1>
            <p role="alert" className="mt-2 text-[13px] leading-relaxed text-[var(--sf-text-dim)]">This SavFlux deployment needs a Supabase project before accounts can sign in.</p>
            <p className="mt-4 rounded-xl border border-[var(--sf-line)] bg-[var(--sf-canvas)] p-3 text-xs leading-relaxed text-[var(--sf-text-mute)]">Set <code>VITE_SUPABASE_URL</code> and <code>VITE_SUPABASE_ANON_KEY</code> in the frontend build environment, and <code>SUPABASE_URL</code> plus <code>SUPABASE_ANON_KEY</code> on the backend. Enable Google and email/password in Supabase Auth.</p>
          </div>
        ) : (
          <>
            <div className="mb-6">
              <div className="mb-3 flex h-10 w-10 items-center justify-center rounded-xl border border-[var(--sf-line)] bg-[var(--sf-raised)] text-[var(--sf-text-dim)]"><LockKeyhole className="h-4 w-4" /></div>
              <h1 className="text-xl font-semibold tracking-tight">{mode === "signup" ? "Create your account" : mode === "reset" ? "Reset your password" : mode === "update-password" ? "Choose a new password" : "Welcome back"}</h1>
              <p className="mt-2 text-[13px] leading-relaxed text-[var(--sf-text-dim)]">{mode === "signup" ? "Create a private SavFlux workspace for your code." : mode === "reset" ? "We’ll email you a secure password-reset link." : mode === "update-password" ? "Set a new password for your SavFlux account." : "Sign in to your private code workspace."}</p>
            </div>

            {(mode === "signin" || mode === "signup") && (
              <button type="button" onClick={() => void signInWithGoogle()} disabled={busy} className="flex h-11 w-full items-center justify-center gap-2 rounded-lg border border-[var(--sf-line-strong)] bg-[var(--sf-surface)] text-sm font-medium text-[var(--sf-text)] transition hover:bg-[var(--sf-raised)] disabled:opacity-50">
                <span aria-hidden="true" className="flex h-5 w-5 items-center justify-center rounded-full bg-white text-[13px] font-bold text-[#4285f4]">G</span>
                Continue with Google
              </button>
            )}

            {(mode === "signin" || mode === "signup") && <div className="my-5 flex items-center gap-3 text-[10px] uppercase tracking-wider text-[var(--sf-text-mute)]"><span className="h-px flex-1 bg-[var(--sf-line)]" />or with email<span className="h-px flex-1 bg-[var(--sf-line)]" /></div>}

            <form onSubmit={submit} className="space-y-3">
              {mode !== "update-password" && <>
                <label htmlFor="savflux-email" className="block text-[12px] font-medium text-[var(--sf-text-dim)]">Email address</label>
                <div className="relative">
                  <Mail className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-[var(--sf-text-mute)]" />
                  <input id="savflux-email" type="email" autoComplete="email" required value={email} onChange={(event) => setEmail(event.target.value)} placeholder="you@example.com" className="h-11 w-full rounded-lg border border-[var(--sf-line-strong)] bg-[var(--sf-canvas)] pl-10 pr-3 text-sm outline-none transition focus:border-[var(--sf-accent)] focus:ring-2 focus:ring-[var(--sf-accent-soft)]" />
                </div>
              </>}
              {mode !== "reset" && <>
                <label htmlFor="savflux-password" className="block pt-1 text-[12px] font-medium text-[var(--sf-text-dim)]">{mode === "update-password" ? "New password" : "Password"}</label>
                <input id="savflux-password" type="password" autoComplete={mode === "signin" ? "current-password" : "new-password"} required minLength={6} value={password} onChange={(event) => setPassword(event.target.value)} placeholder="At least 6 characters" className="h-11 w-full rounded-lg border border-[var(--sf-line-strong)] bg-[var(--sf-canvas)] px-3 text-sm outline-none transition focus:border-[var(--sf-accent)] focus:ring-2 focus:ring-[var(--sf-accent-soft)]" />
              </>}
              <button type="submit" disabled={busy || (mode !== "update-password" && !email.trim()) || (mode !== "reset" && !password)} className="flex h-11 w-full items-center justify-center gap-2 rounded-lg bg-[var(--sf-accent)] px-4 text-sm font-medium text-white transition hover:brightness-110 disabled:cursor-not-allowed disabled:opacity-50">
                {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : null}{busy ? "Please wait…" : mode === "signup" ? "Create account" : mode === "reset" ? "Send reset link" : mode === "update-password" ? "Update password" : "Sign in"}{!busy && <ArrowRight className="h-4 w-4" />}
              </button>
            </form>

            {error && <p role="alert" className="mt-4 rounded-lg border border-red-400/20 bg-red-400/5 px-3 py-2.5 text-[12px] leading-relaxed text-red-300">{error}</p>}
            {notice && <p role="status" className="mt-4 rounded-lg border border-emerald-400/20 bg-emerald-400/5 px-3 py-2.5 text-[12px] leading-relaxed text-emerald-300">{notice}</p>}
            <div className="mt-5 flex flex-wrap items-center justify-between gap-2 text-[12px] text-[var(--sf-text-dim)]">
              {mode === "reset" || mode === "update-password" ? <button type="button" onClick={() => { if (mode === "update-password") void supabase?.auth.signOut(); setMode("signin"); setPassword(""); setError(""); setNotice(""); }} className="inline-flex items-center gap-1 hover:text-[var(--sf-accent)]"><ArrowLeft className="h-3 w-3" /> Back to sign in</button> : <>
                <button type="button" onClick={() => { setMode(mode === "signup" ? "signin" : "signup"); setError(""); setNotice(""); }} className="hover:text-[var(--sf-accent)]">{mode === "signup" ? "Already have an account? Sign in" : "Create an account"}</button>
                {mode === "signin" && <button type="button" onClick={() => { setMode("reset"); setError(""); setNotice(""); }} className="hover:text-[var(--sf-accent)]">Forgot password?</button>}
              </>}
            </div>
            {error && state === "unavailable" && <button type="button" onClick={() => void check()} className="mt-3 text-xs text-[var(--sf-accent)] hover:underline">Retry connection</button>}
            <p className="mt-6 border-t border-[var(--sf-line)] pt-4 text-[11px] leading-relaxed text-[var(--sf-text-mute)]">Each account has its own private code index, GitHub connection, provider keys, and Agent workspace. SavFlux verifies your session with Supabase and does not store your password.</p>
          </>
        )}
      </section>
    </main>
  );
}
