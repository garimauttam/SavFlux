import { useCallback, useEffect, useState, type FormEvent, type ReactNode } from "react";
import { ArrowRight, KeyRound, LockKeyhole, ShieldCheck } from "lucide-react";
import { apiFetch, clearOwnerKey, getOwnerKey, setOwnerKey } from "../api";

type GateState = "checking" | "signed-out" | "signed-in" | "unavailable";

/**
 * A small single-owner sign-in gate, not a registration flow.
 * The owner key is kept only in sessionStorage and is cleared when this browser
 * tab's session ends or the owner explicitly signs out. It is never displayed
 * again after entry and is never sent to analytics or logged by the frontend.
 */
export function AuthGate({ children }: { children: ReactNode }) {
  const [state, setState] = useState<GateState>("checking");
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const check = useCallback(async () => {
    const saved = getOwnerKey();
    if (!saved) {
      setState("signed-out");
      return;
    }
    setState("checking");
    try {
      const response = await apiFetch("/api/v1/auth/session", { method: "POST" });
      if (response.ok) {
        setState("signed-in");
        setError("");
      } else if (response.status === 401) {
        clearOwnerKey();
        setState("signed-out");
        setError("That owner key was not accepted. Check it and try again.");
      } else {
        setState("unavailable");
        setError(`SavFlux could not verify this session (HTTP ${response.status}).`);
      }
    } catch {
      setState("unavailable");
      setError("Could not reach the SavFlux server. Check the connection and retry.");
    }
  }, []);

  useEffect(() => {
    void check();
    const onSignOut = () => {
      clearOwnerKey();
      setKey("");
      setError("");
      setState("signed-out");
    };
    window.addEventListener("savflux:signout", onSignOut);
    return () => window.removeEventListener("savflux:signout", onSignOut);
  }, [check]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const candidate = key.trim();
    if (!candidate || busy) return;
    setBusy(true);
    setError("");
    setOwnerKey(candidate);
    try {
      const response = await apiFetch("/api/v1/auth/session", { method: "POST" });
      if (!response.ok) {
        clearOwnerKey();
        setError(response.status === 401
          ? "That owner key was not accepted. Check it and try again."
          : `SavFlux could not verify the key (HTTP ${response.status}).`);
        setState("signed-out");
        return;
      }
      setKey("");
      setState("signed-in");
    } catch {
      clearOwnerKey();
      setState("signed-out");
      setError("Could not reach the SavFlux server. Check the connection and retry.");
    } finally {
      setBusy(false);
    }
  }

  if (state === "signed-in") return <>{children}</>;

  return (
    <main className="flex min-h-full items-center justify-center bg-[var(--sf-canvas)] px-5 py-12 text-[var(--sf-text)]">
      <section className="w-full max-w-[420px] rounded-2xl border border-[var(--sf-line)] bg-[var(--sf-surface)] p-7 shadow-2xl shadow-black/20 sm:p-9">
        <div className="mb-8 flex items-center gap-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-[var(--sf-accent-soft)] text-[var(--sf-accent)]">
            <ShieldCheck className="h-5 w-5" />
          </div>
          <div>
            <p className="text-[15px] font-semibold tracking-tight">SavFlux</p>
            <p className="text-[12px] text-[var(--sf-text-mute)]">Private code workspace</p>
          </div>
        </div>

        {state === "checking" ? (
          <div role="status" className="py-8 text-center text-sm text-[var(--sf-text-dim)]">
            Checking your session…
          </div>
        ) : (
          <>
            <div className="mb-6">
              <div className="mb-3 flex h-10 w-10 items-center justify-center rounded-xl border border-[var(--sf-line)] bg-[var(--sf-raised)] text-[var(--sf-text-dim)]">
                <LockKeyhole className="h-4 w-4" />
              </div>
              <h1 className="text-xl font-semibold tracking-tight">Sign in to your instance</h1>
              <p className="mt-2 text-[13px] leading-relaxed text-[var(--sf-text-dim)]">
                SavFlux is single-owner by default. Enter the private owner key for this instance.
              </p>
            </div>

            <form onSubmit={submit} className="space-y-3">
              <label htmlFor="savflux-owner-key" className="block text-[12px] font-medium text-[var(--sf-text-dim)]">
                Instance owner key
              </label>
              <div className="relative">
                <KeyRound className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-[var(--sf-text-mute)]" />
                <input
                  id="savflux-owner-key"
                  type="password"
                  autoComplete="current-password"
                  autoCapitalize="none"
                  spellCheck={false}
                  required
                  value={key}
                  onChange={(event) => setKey(event.target.value)}
                  placeholder="Paste your owner key"
                  className="h-11 w-full rounded-lg border border-[var(--sf-line-strong)] bg-[var(--sf-canvas)] pl-10 pr-3 text-sm outline-none transition focus:border-[var(--sf-accent)] focus:ring-2 focus:ring-[var(--sf-accent-soft)]"
                  aria-describedby={error ? "savflux-auth-error" : "savflux-key-help"}
                  autoFocus
                />
              </div>
              <button
                type="submit"
                disabled={busy || !key.trim()}
                className="flex h-11 w-full items-center justify-center gap-2 rounded-lg bg-[var(--sf-accent)] px-4 text-sm font-medium text-white transition hover:brightness-110 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {busy ? "Verifying…" : "Continue"}
                {!busy && <ArrowRight className="h-4 w-4" />}
              </button>
            </form>

            {error && (
              <p id="savflux-auth-error" role="alert" className="mt-4 rounded-lg border border-red-400/20 bg-red-400/5 px-3 py-2.5 text-[12px] leading-relaxed text-red-300">
                {error}
              </p>
            )}
            {state === "unavailable" && (
              <button type="button" onClick={() => void check()} className="mt-3 text-xs text-[var(--sf-accent)] hover:underline">
                Retry connection
              </button>
            )}
            <p id="savflux-key-help" className="mt-5 border-t border-[var(--sf-line)] pt-4 text-[11px] leading-relaxed text-[var(--sf-text-mute)]">
              On a local install, the key is printed once in the backend startup log and saved as <code className="rounded bg-[var(--sf-raised)] px-1 py-0.5">chroma_data/owner_key</code>. For a hosted install, the operator can set <code className="rounded bg-[var(--sf-raised)] px-1 py-0.5">API_KEY</code> in the deployment environment. There are no public sign-ups or user accounts in this single-owner mode.
            </p>
          </>
        )}
      </section>
    </main>
  );
}
