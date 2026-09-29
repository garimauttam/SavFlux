import { useEffect, useState, type ReactNode } from "react";
import { Github, LogOut, ShieldCheck, UserRound } from "lucide-react";
import { supabase } from "../../lib/supabase";
import { useAuthUserId } from "../../lib/authUser";
import type { GitHubStatus } from "../../types/workspace";

interface Props {
  github: GitHubStatus | null;
  onConnectGitHub: () => void;
  modelsContent: ReactNode;
  isDark: boolean;
  onToggleTheme: () => void;
}
const tabs = [
  "Account",
  "Connections",
  "Models",
  "Appearance",
  "Privacy",
] as const;
export function ProfilePanel({
  github,
  onConnectGitHub,
  modelsContent,
  isDark,
  onToggleTheme,
}: Props) {
  const userId = useAuthUserId();
  const [tab, setTab] = useState<(typeof tabs)[number]>("Account");
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [ready, setReady] = useState(false);
  const [feedback, setFeedback] = useState("");
  useEffect(() => {
    let cancelled = false;
    void supabase?.auth
      .getSession()
      .then(({ data, error }) => {
        if (cancelled) return;
        if (error || data.session?.user.id !== userId) {
          setFeedback("Could not load your account. Please sign in again.");
          return;
        }
        setEmail(data.session.user.email ?? "");
        setName(data.session.user.user_metadata?.display_name ?? "");
        setReady(true);
      })
      .catch(() => {
        if (!cancelled) setFeedback("Could not load your profile.");
      });
    return () => {
      cancelled = true;
    };
  }, [userId]);
  const save = async () => {
    if (!supabase || !ready) return;
    setBusy(true);
    setFeedback("");
    try {
      const { error } = await supabase.auth.updateUser({
        data: { display_name: name.trim() },
      });
      setFeedback(
        error
          ? "Could not save your display name. Please try again."
          : "Profile saved.",
      );
    } catch {
      setFeedback("Could not save your profile. Check your connection.");
    } finally {
      setBusy(false);
    }
  };
  const connected = Boolean(github?.connected && github.valid);
  return (
    <div className="h-full overflow-y-auto sf-surface">
      <div className="mx-auto max-w-4xl px-5 py-8 sm:px-8">
        <p className="sf-mute text-xs uppercase tracking-widest">
          Your workspace
        </p>
        <h1 className="sf-text mt-2 text-2xl font-semibold">
          Profile & settings
        </h1>
        <p className="sf-dim mt-2 text-sm">
          Manage your account, connections and how SavFlux works for you.
        </p>
        <div
          role="tablist"
          aria-label="Profile settings"
          className="mt-7 flex gap-1 overflow-x-auto border-b sf-line pb-2"
        >
          {tabs.map((t) => (
            <button
              key={t}
              role="tab"
              aria-selected={tab === t}
              onClick={() => setTab(t)}
              className={`sf-btn shrink-0 ${tab === t ? "sf-accent-soft sf-accent" : "sf-btn-ghost"}`}
            >
              {t}
            </button>
          ))}
        </div>
        <div
          role="tabpanel"
          aria-label={tab}
          className="mt-6 rounded-xl border sf-line sf-surface p-5"
        >
          {tab === "Account" && (
            <>
              <div className="mb-6 flex items-center gap-3">
                <div className="sf-accent-soft sf-accent rounded-full p-3">
                  <UserRound size={22} />
                </div>
                <div>
                  <h2 className="sf-text font-medium">Your profile</h2>
                  <p className="sf-dim text-xs">{email || "SavFlux account"}</p>
                </div>
              </div>
              <form
                onSubmit={(e) => {
                  e.preventDefault();
                  void save();
                }}
                className="max-w-md space-y-4"
              >
                <label className="sf-dim block text-sm">
                  Display name
                  <input
                    className="sf-input mt-2"
                    value={name}
                    maxLength={80}
                    onChange={(e) => setName(e.target.value)}
                    disabled={!ready || busy}
                    autoComplete="nickname"
                  />
                </label>
                <label className="sf-dim block text-sm">
                  Email
                  <input className="sf-input mt-2" value={email} readOnly />
                </label>
                <button
                  className="sf-btn sf-btn-primary"
                  disabled={!ready || busy}
                >
                  {busy ? "Saving…" : "Save profile"}
                </button>
                {feedback && (
                  <p role="status" className="sf-dim text-sm">
                    {feedback}
                  </p>
                )}
              </form>
              <div className="mt-8 border-t sf-line pt-5">
                <button
                  className="sf-btn sf-btn-secondary"
                  onClick={() =>
                    window.dispatchEvent(new Event("savflux:signout"))
                  }
                >
                  <LogOut size={14} /> Sign out of SavFlux
                </button>
              </div>
            </>
          )}
          {tab === "Connections" && (
            <>
              <div className="flex items-center gap-3">
                <Github size={24} />
                <div>
                  <h2 className="sf-text font-medium">GitHub</h2>
                  <p className="sf-dim text-sm">
                    {connected
                      ? `Connected as @${github?.user?.login}`
                      : "Not connected"}
                  </p>
                </div>
                <span
                  className={`sf-chip ml-auto ${connected ? "sf-chip-good" : ""}`}
                >
                  {connected ? "Connected" : "Optional"}
                </span>
              </div>
              <p className="sf-dim my-5 max-w-lg text-sm">
                Connect to browse private repositories and open pull requests.
                Public repositories can still be indexed without connecting.
                Your GitHub connection is separate from your SavFlux login.
              </p>
              <button
                className="sf-btn sf-btn-primary"
                onClick={onConnectGitHub}
              >
                {connected ? "Manage GitHub connection" : "Connect GitHub"}
              </button>
            </>
          )}
          {tab === "Models" && modelsContent}
          {tab === "Appearance" && (
            <>
              <h2 className="sf-text font-medium">Appearance</h2>
              <p className="sf-dim my-3 text-sm">
                Theme and panel preferences are saved in this browser. Use the
                panel buttons to collapse the navigation, file list or review
                notes. Reduced-motion system preferences are respected.
              </p>
              <button
                className="sf-btn sf-btn-secondary"
                onClick={onToggleTheme}
              >
                Switch to {isDark ? "light" : "dark"} theme
              </button>
            </>
          )}
          {tab === "Privacy" && (
            <>
              <h2 className="sf-text flex items-center gap-2 font-medium">
                <ShieldCheck size={18} /> Your data & permissions
              </h2>
              <div className="sf-dim mt-4 space-y-4 text-sm leading-relaxed">
                <p>
                  <strong className="sf-text">
                    Personalization is not enabled.
                  </strong>{" "}
                  Having an account is not consent to reuse your code or reviews
                  for personalization. There is no background personalization
                  collection added here.
                </p>
                <p>
                  New reviews do not read or write cached review results.
                  Indexed code and settings needed to operate the app are
                  separate. Inline comment resolution is kept only for the
                  current review view, not saved as a profile of you.
                </p>
                <p>
                  Choosing a cloud model sends review context to that provider.
                  Choose Ollama in Models for local model calls. GitHub and
                  provider credentials are managed through their connection
                  settings, not your display name.
                </p>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
