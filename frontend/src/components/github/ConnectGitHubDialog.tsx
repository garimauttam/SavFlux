/**
 * ConnectGitHubDialog.tsx — connect an account without leaving the product.
 *
 * WHY THIS IS A DIALOG AND NOT "PASTE A URL IN THE SIDEBAR"
 * ----------------------------------------------------------
 * The old sidebar asked for a *repository URL*. That is the shape of a tool for
 * indexing public code, and it put three separate jobs behind one text box:
 * proving who you are, choosing what to work on, and choosing which branch.
 * A user with ten repositories had to know and type the exact URL of the one
 * they meant, and a private repository simply failed with a clone error that
 * said nothing about credentials.
 *
 * So the order is now the order the decisions actually happen in: connect an
 * account, then pick a repository from a list, then pick a branch.
 *
 * The token is validated against GitHub *before* it is stored. That is the
 * whole reason this dialog can tell you the token is wrong instead of
 * discovering it on the first clone.
 */

import { useEffect, useRef, useState } from "react";
import {
  AlertTriangle,
  Check,
  ExternalLink,
  Eye,
  EyeOff,
  Github,
  Info,
  Loader2,
  LogOut,
  ShieldCheck,
  X,
} from "lucide-react";
import type { GitHubStatus } from "../../types/workspace";

interface ConnectGitHubDialogProps {
  open: boolean;
  onClose: () => void;
  status: GitHubStatus | null;
  busy: boolean;
  error: string | null;
  onConnect: (token: string) => Promise<boolean>;
  onDisconnect: () => Promise<void>;
}

export function ConnectGitHubDialog({
  open,
  onClose,
  status,
  busy,
  error,
  onConnect,
  onDisconnect,
}: ConnectGitHubDialogProps) {
  const [token, setToken] = useState("");
  const [reveal, setReveal] = useState(false);
  const [touched, setTouched] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (open) {
      setToken("");
      setReveal(false);
      setTouched(false);
      // Focus lands on the field: the only thing to do in this dialog is type.
      window.setTimeout(() => inputRef.current?.focus(), 60);
    }
  }, [open]);

  if (!open) return null;

  const connected = Boolean(status?.connected && status?.valid);
  const tooShort = token.trim().length > 0 && token.trim().length < 8;

  const submit = async () => {
    setTouched(true);
    if (token.trim().length < 8) return;
    const ok = await onConnect(token.trim());
    if (ok) {
      setToken("");
      onClose();
    }
  };

  return (
    <div
      className="fixed inset-0 z-[100] flex items-center justify-center p-4"
      style={{ background: "rgba(0,0,0,0.6)", backdropFilter: "blur(3px)" }}
      role="dialog"
      aria-modal="true"
      aria-labelledby="gh-dialog-title"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        className="w-full max-w-[480px] overflow-hidden rounded-2xl border sf-line shadow-2xl shadow-black/60 sf-surface"
        onKeyDown={(e) => {
          if (e.key === "Escape") onClose();
        }}
      >
        <div className="flex items-start justify-between gap-3 border-b sf-line px-5 py-4">
          <div>
            <h2 id="gh-dialog-title" className="sf-text flex items-center gap-2 text-[15px] font-semibold">
              <Github className="h-4 w-4" /> Connect GitHub
            </h2>
            <p className="sf-mute mt-1 text-[12px] leading-relaxed">
              Your repositories, pull requests, and private code — without leaving SavFlux.
            </p>
          </div>
          <button type="button" onClick={onClose} aria-label="Close" className="sf-iconbtn h-7 w-7">
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="space-y-4 px-5 py-4">
          {connected ? (
            <div className="flex items-center gap-3 rounded-xl border sf-line sf-raised px-4 py-3">
              {status?.user?.avatar_url ? (
                <img
                  src={status.user.avatar_url}
                  alt=""
                  className="h-9 w-9 rounded-full ring-1 ring-white/15"
                  referrerPolicy="no-referrer"
                />
              ) : (
                <div className="sf-iconbtn h-9 w-9">
                  <Github className="h-4 w-4" />
                </div>
              )}
              <div className="min-w-0 flex-1">
                <p className="sf-text truncate text-[13px] font-medium">
                  {status?.user?.name || status?.user?.login}
                </p>
                <p className="sf-mute truncate text-[11.5px]">
                  @{status?.user?.login}
                  {status?.source === "env" && " · from GITHUB_TOKEN"}
                </p>
              </div>
              <span className="sf-chip sf-chip-good shrink-0">
                <Check className="h-3 w-3" /> connected
              </span>
            </div>
          ) : (
            <>
              <div>
                <label htmlFor="gh-token" className="sf-dim mb-1.5 block text-[12px] font-medium">
                  Personal access token
                </label>
                <div className="relative">
                  <input
                    id="gh-token"
                    ref={inputRef}
                    type={reveal ? "text" : "password"}
                    value={token}
                    onChange={(e) => setToken(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") void submit();
                    }}
                    placeholder="ghp_…  or  github_pat_…"
                    autoComplete="off"
                    spellCheck={false}
                    className="sf-input sf-mono pr-9"
                    aria-invalid={touched && token.trim().length < 8}
                  />
                  <button
                    type="button"
                    onClick={() => setReveal((v) => !v)}
                    aria-label={reveal ? "Hide token" : "Show token"}
                    title={reveal ? "Hide" : "Show"}
                    className="sf-iconbtn absolute right-1.5 top-1.5 h-6 w-6"
                  >
                    {reveal ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
                  </button>
                </div>
                {touched && tooShort && (
                  <p className="mt-1.5 text-[11.5px]" style={{ color: "var(--sf-bad)" }}>
                    A GitHub token is at least 8 characters. This looks like a paste that was cut short.
                  </p>
                )}
              </div>

              {/* Do I even need a token?
                  The honest answer for most people is no, and saying so here
                  saves the whole dialog: a public repository is read through
                  GitHub's unauthenticated API with nothing stored and nothing
                  connected. A token is for private code, and for writing. */}
              <div
                className="rounded-xl border px-3.5 py-3"
                style={{ borderColor: "var(--sf-line)", background: "var(--sf-raised)" }}
              >
                <p className="sf-dim flex items-center gap-1.5 text-[12px] font-medium">
                  <Info className="h-3.5 w-3.5" /> You may not need one
                </p>
                <p className="sf-mute mt-1.5 text-[11.5px] leading-relaxed">
                  A <strong className="sf-text">public</strong> repository needs no account at
                  all — close this and paste its URL under <em>Repositories → Without an
                  account</em>. A token is for <strong className="sf-text">private</strong> code,
                  and for opening pull requests.
                </p>
              </div>

              <div className="rounded-xl border sf-line sf-raised px-3.5 py-3">
                <p className="sf-dim flex items-center gap-1.5 text-[12px] font-medium">
                  <ShieldCheck className="h-3.5 w-3.5" /> Pick the permissions you actually want
                </p>
                <ul className="sf-mute mt-1.5 space-y-2 text-[11.5px] leading-relaxed">
                  <li>
                    <strong className="sf-text">Read-only</strong> — browse repositories, index
                    private code, ask questions, review. Create PR stays hidden. Choose a{" "}
                    <em>fine-grained</em> token with Contents → <em>Read-only</em> and nothing else.
                  </li>
                  <li>
                    <strong className="sf-text">Read and write</strong> — everything above, plus
                    pushing a branch and opening a pull request after you confirm a diff. Needs
                    Contents → <em>Read and write</em> and Pull requests → <em>Read and write</em>.
                  </li>
                </ul>
                <p className="sf-mute mt-2 text-[11px] leading-relaxed">
                  SavFlux asks GitHub what the token can actually do on each repository and hides
                  the controls it cannot perform, so a read-only token never offers a push that
                  would fail. The token is stored on this machine only (
                  <code className="sf-mono">chroma_data/github_token</code>, mode 0600) and is
                  never sent anywhere except api.github.com.
                </p>
              </div>
            </>
          )}

          {error && (
            <div
              role="alert"
              className="flex items-start gap-2 rounded-xl border px-3 py-2.5 text-[12px]"
              style={{
                borderColor: "rgba(248,113,113,0.3)",
                background: "rgba(248,113,113,0.1)",
                color: "#fca5a5",
              }}
            >
              <AlertTriangle className="mt-px h-3.5 w-3.5 shrink-0" />
              <span>{error}</span>
            </div>
          )}

          {status?.connected && !status?.valid && !error && (
            <div
              className="flex items-start gap-2 rounded-xl border px-3 py-2.5 text-[12px]"
              style={{
                borderColor: "rgba(251,191,36,0.3)",
                background: "rgba(251,191,36,0.08)",
                color: "#fcd34d",
              }}
            >
              <AlertTriangle className="mt-px h-3.5 w-3.5 shrink-0" />
              <span>{status.message}</span>
            </div>
          )}
        </div>

        <div className="flex items-center justify-between gap-2 border-t sf-line px-5 py-3">
          {connected ? (
            <button
              type="button"
              onClick={() => void onDisconnect()}
              disabled={busy}
              className="sf-btn sf-btn-danger"
            >
              <LogOut className="h-3.5 w-3.5" /> Disconnect
            </button>
          ) : (
            <div className="flex items-center gap-1.5">
              <a
                href="https://github.com/settings/personal-access-tokens/new"
                target="_blank"
                rel="noopener noreferrer"
                className="sf-btn sf-btn-ghost"
                title="Fine-grained token. Choose the permissions on the next screen."
              >
                Create a token <ExternalLink className="h-3 w-3" />
              </a>
              {/* The button above opens github.com/login first when the browser
                  is not already signed in — that is GitHub's own redirect, not a
                  broken link, and it is the single most confusing moment in this
                  flow. Said here, it stops reading as an error. */}
              <a
                href="https://github.com/login"
                target="_blank"
                rel="noopener noreferrer"
                className="sf-btn sf-btn-ghost"
                title="GitHub sends you here first if you are not signed in"
              >
                Sign in
              </a>
            </div>
          )}
          <div className="flex items-center gap-2">
            <button type="button" onClick={onClose} className="sf-btn sf-btn-secondary">
              {connected ? "Done" : "Cancel"}
            </button>
            {!connected && (
              <button
                type="button"
                onClick={() => void submit()}
                disabled={busy || token.trim().length < 8}
                className="sf-btn sf-btn-primary"
              >
                {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Github className="h-3.5 w-3.5" />}
                Connect
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
