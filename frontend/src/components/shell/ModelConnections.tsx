import { useEffect, useRef, useState } from "react";
import { Cloud, KeyRound, RefreshCw, ShieldCheck, Trash2 } from "lucide-react";
import { apiFetch } from "../../api";
import type { ModelStatus } from "../../types/workspace";

type Provider = "groq" | "openrouter" | "gemini" | "mistral";
type Task = "chat" | "coding" | "review" | "reasoning";
interface Model {
  id: string;
  name: string;
  reasoning: string[];
  context_length?: number;
  cost: string;
}
interface Connection {
  model: string;
  reasoning: string;
  reasoning_options: string[];
}
interface ProviderInfo {
  id: Provider;
  label: string;
  note: string;
  key_url: string;
  limits_url: string;
  key_configured: boolean;
  key_source: string | null;
  selection?: Connection;
}
interface Routing {
  mode: "single" | "tasks";
  single: string;
  tasks: Partial<Record<Task, string>>;
}
interface State {
  providers: ProviderInfo[];
  routing: Routing;
  managed: boolean;
}
const tasks: { id: Task; label: string }[] = [
  { id: "chat", label: "Chat / Q&A" },
  { id: "coding", label: "Code generation" },
  { id: "review", label: "Code review / fixes" },
  { id: "reasoning", label: "Repository reasoning / summaries" },
];
async function result(res: Response) {
  const data = await res.json();
  if (!res.ok)
    throw new Error(
      typeof data.detail === "string"
        ? data.detail
        : "Could not update model settings.",
    );
  return data;
}

export function ModelConnections({ models }: { models: ModelStatus | null }) {
  const [state, setState] = useState<State | null>(null);
  const [provider, setProvider] = useState<Provider>("groq");
  const [key, setKey] = useState("");
  const [catalog, setCatalog] = useState<Model[]>([]);
  const [model, setModel] = useState("");
  const [reasoning, setReasoning] = useState("default");
  const [consent, setConsent] = useState(false);
  const [freePlan, setFreePlan] = useState(false);
  const [routing, setRouting] = useState<Routing>({
    mode: "single",
    single: "ollama",
    tasks: {},
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [feedback, setFeedback] = useState("");
  const controller = useRef<AbortController | null>(null);
  useEffect(() => {
    window.dispatchEvent(new Event("savflux:models-changed"));
    const c = new AbortController();
    void apiFetch("/api/v1/models/connections", { signal: c.signal })
      .then(result)
      .then((data: State) => {
        if (!c.signal.aborted) {
          if (!Array.isArray(data.providers) || !data.routing)
            throw new Error(
              "Connection API is unavailable. Update and restart the backend.",
            );
          setState(data);
          setRouting(data.routing);
          const saved = data.providers.find((p) => p.id === "groq")?.selection;
          setModel(saved?.model ?? "");
          setReasoning(saved?.reasoning ?? "default");
        }
      })
      .catch((e) => {
        if (!c.signal.aborted) setError(e.message);
      });
    return () => {
      c.abort();
      controller.current?.abort();
    };
  }, []);
  const info = state?.providers.find((p) => p.id === provider);
  const chosen = catalog.find((m) => m.id === model);
  const options =
    chosen?.reasoning ??
    (info?.selection?.model === model ? info.selection.reasoning_options : []);
  const connected =
    state?.providers.filter((p) => p.selection && p.key_configured) ?? [];
  const chooseProvider = (p: Provider) => {
    controller.current?.abort();
    setBusy(false);
    setProvider(p);
    setKey("");
    setCatalog([]);
    setConsent(false);
    setFreePlan(false);
    setFeedback("");
    setError("");
    const saved = state?.providers.find((v) => v.id === p)?.selection;
    setModel(saved?.model ?? "");
    setReasoning(saved?.reasoning ?? "default");
  };
  const action = async (
    path: string,
    method: string,
    body?: unknown,
    apply = true,
  ) => {
    const c = new AbortController();
    controller.current?.abort();
    controller.current = c;
    setBusy(true);
    setError("");
    setFeedback("");
    try {
      const data = await result(
        await apiFetch(`/api/v1/models/${path}`, {
          method,
          signal: c.signal,
          headers: { "Content-Type": "application/json" },
          body: body === undefined ? undefined : JSON.stringify(body),
        }),
      );
      if (c.signal.aborted) return;
      if (apply) {
        if (!Array.isArray(data.providers) || !data.routing)
          throw new Error(
            "Connection API is unavailable. Update and restart the backend.",
          );
        setState(data);
        setRouting(data.routing);
        window.dispatchEvent(new Event("savflux:models-changed"));
      }
      return data;
    } catch (e) {
      if (!c.signal.aborted)
        setError(e instanceof Error ? e.message : "Request failed.");
    } finally {
      if (!c.signal.aborted) setBusy(false);
    }
  };
  const loadCatalog = async () => {
    const data = await action(
      "catalog",
      "POST",
      { provider, api_key: key || undefined },
      false,
    );
    if (data) {
      setCatalog(data.models);
      setFeedback(
        data.models.length
          ? `${data.models.length} eligible models loaded. This did not generate text or store the pasted key.`
          : "No eligible free-tier models returned. Check this account's access or choose another provider.",
      );
    }
  };
  const save = async () => {
    const data = await action("connections", "PUT", {
      provider,
      api_key: key || undefined,
      model,
      reasoning,
      consent,
      free_plan_confirmed: freePlan,
    });
    if (data) {
      setKey("");
      setConsent(false);
      setFeedback(
        "Connection saved. Choose its route below and save routing to use it. Other provider keys were kept.",
      );
    }
  };
  const probe = async (p: string) => {
    const data = await action("test", "POST", { provider: p }, false);
    if (data) setFeedback(data.message);
    window.dispatchEvent(new Event("savflux:models-changed"));
  };
  return (
    <section
      aria-label="Model connections"
      className="mt-4 space-y-4 rounded-xl border sf-line sf-raised p-4"
    >
      <div className="flex items-start gap-2">
        <Cloud size={18} className="sf-accent mt-1 shrink-0" />
        <div>
          <h3 className="sf-text text-sm font-semibold">Model connections</h3>
          <p className="sf-dim mt-1 text-xs">
            Bring multiple keys. Choose a single model, or send each task to a
            different connected provider. Local Ollama is the only automatic
            fallback.
          </p>
        </div>
      </div>
      <p className="sf-mute text-xs">
        Free quotas are not unlimited. SavFlux cannot detect your provider’s
        billing plan. Use free-tier accounts without paid billing; no paid model
        upgrades or key rotation are performed.
      </p>
      {!state && (
        <button
          className="sf-btn sf-btn-secondary text-xs"
          onClick={() => void action("connections", "GET")}
        >
          Load connection settings
        </button>
      )}
      {state && (
        <>
          <div className="grid grid-cols-2 gap-2">
            {state.providers.map((p) => (
              <button
                key={p.id}
                className={`sf-btn justify-start text-xs ${provider === p.id ? "sf-accent-soft sf-accent" : "sf-btn-secondary"}`}
                aria-pressed={provider === p.id}
                onClick={() => chooseProvider(p.id)}
              >
                <KeyRound size={13} />
                <span>
                  {p.label}
                  <span className="sf-mute block text-[10px]">
                    {p.key_configured
                      ? p.selection
                        ? "Connected"
                        : "Key available · choose model"
                      : "Not connected"}
                  </span>
                </span>
              </button>
            ))}
          </div>
          <div className="space-y-3 rounded-lg border sf-line sf-surface p-3">
            <div>
              <h4 className="sf-text text-xs font-semibold">{info?.label}</h4>
              <p className="sf-dim mt-1 text-xs">{info?.note}</p>
              <div className="mt-2 flex gap-3 text-xs sf-accent">
                <a href={info?.key_url} target="_blank" rel="noreferrer">
                  Get API key ↗
                </a>
                <a href={info?.limits_url} target="_blank" rel="noreferrer">
                  Check current limits ↗
                </a>
              </div>
            </div>
            <label className="sf-dim block text-xs">
              Provider API key
              <input
                aria-label="Provider API key"
                type="password"
                autoComplete="new-password"
                spellCheck={false}
                className="sf-input mt-1"
                value={key}
                maxLength={500}
                onChange={(e) => {
                  setKey(e.target.value);
                  setCatalog([]);
                }}
                placeholder={
                  info?.key_configured
                    ? "Saved server-side · leave blank to retain"
                    : "Paste key here, not in chat"
                }
              />
            </label>
            <button
              className="sf-btn sf-btn-secondary text-xs"
              disabled={busy || (!key && !info?.key_configured)}
              onClick={() => void loadCatalog()}
            >
              <RefreshCw size={13} /> Load / refresh models
            </button>
            <label className="sf-dim block text-xs">
              Cloud model
              <select
                aria-label="Cloud model"
                className="sf-input mt-1"
                value={model}
                disabled={busy}
                onChange={(e) => {
                  setModel(e.target.value);
                  setReasoning("default");
                }}
              >
                <option value="">Choose from the live catalog…</option>
                {model && !catalog.some((m) => m.id === model) && (
                  <option value={model}>
                    {model} · saved; refresh to verify
                  </option>
                )}
                {catalog.map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.id}
                  </option>
                ))}
              </select>
            </label>
            <label className="sf-dim block text-xs">
              Reasoning effort
              <select
                aria-label="Reasoning effort"
                className="sf-input mt-1"
                value={reasoning}
                disabled={busy || !options.length}
                onChange={(e) => setReasoning(e.target.value)}
              >
                <option value="default">Provider default</option>
                {options.map((v) => (
                  <option key={v} value={v}>
                    {v}
                  </option>
                ))}
              </select>
            </label>
            <p className="sf-mute text-[11px]">
              {options.length
                ? "Only documented effort levels are offered. Higher reasoning consumes more quota and time."
                : "No verified effort control for this model. Provider defaults will be used."}
              {chosen?.context_length
                ? ` Context window: ${chosen.context_length.toLocaleString()} tokens; your rate limit may be much lower.`
                : ""}
            </p>
            <label className="sf-dim flex items-start gap-2 text-xs">
              <input
                type="checkbox"
                checked={consent}
                onChange={(e) => setConsent(e.target.checked)}
                className="mt-1"
              />
              I consent to storing this provider key/settings on this server and
              sending task code/context to this provider when I select it.
              Free-tier data policies may allow retention or training.
            </label>
            {provider !== "openrouter" && (
              <label className="sf-dim flex items-start gap-2 text-xs">
                <input
                  type="checkbox"
                  checked={freePlan}
                  onChange={(e) => setFreePlan(e.target.checked)}
                  className="mt-1"
                />
                I checked that this account/project uses the free tier, not paid
                billing.
              </label>
            )}
            <p className="sf-mute text-[11px]">
              Keys are stored per account in server files with restricted
              permissions, not browser storage. Server administrators can access
              these files; storage is not encrypted at rest by SavFlux.
            </p>
            <div className="flex flex-wrap gap-2">
              <button
                className="sf-btn sf-btn-primary text-xs"
                disabled={
                  busy ||
                  !model ||
                  !consent ||
                  (provider !== "openrouter" && !freePlan)
                }
                onClick={() => void save()}
              >
                Save connection
              </button>
              {info?.selection && (
                <>
                  <button
                    className="sf-btn sf-btn-secondary text-xs"
                    disabled={busy}
                    onClick={() => void probe(provider)}
                  >
                    Test saved model
                  </button>
                  <button
                    className="sf-btn sf-btn-ghost text-xs"
                    disabled={busy}
                    onClick={() => {
                      if (
                        window.confirm(
                          `Remove ${info.label} connection and saved key? Its task routes will switch to Ollama. Environment keys are unchanged.`,
                        )
                      )
                        void action(`connections/${provider}`, "DELETE").then(
                          (data) => {
                            if (data) {
                              setKey("");
                              setModel("");
                              setCatalog([]);
                              setFeedback(
                                "Connection removed; affected routes now use Ollama.",
                              );
                            }
                          },
                        );
                    }}
                  >
                    <Trash2 size={13} />
                    Remove
                  </button>
                </>
              )}
            </div>
            <p className="sf-mute text-[11px]">
              Test sends a small “Reply OK” prompt and consumes provider quota.
              It never sends repository code.
            </p>
          </div>
          <div className="space-y-3 border-t sf-line pt-3">
            <h4 className="sf-text flex items-center gap-2 text-xs font-semibold">
              <ShieldCheck size={14} /> Task routing
            </h4>
            {!state.managed && (
              <p className="sf-dim text-xs">
                Existing model configuration is still active. Saving below
                explicitly enables this routing system.
              </p>
            )}
            <label className="sf-dim block text-xs">
              Routing mode
              <select
                aria-label="Routing mode"
                className="sf-input mt-1"
                value={routing.mode}
                onChange={(e) =>
                  setRouting({
                    ...routing,
                    mode: e.target.value as Routing["mode"],
                  })
                }
              >
                <option value="single">One model for all tasks</option>
                <option value="tasks">Assign providers by task</option>
              </select>
            </label>
            {[
              { id: "single", label: "Default model" },
              ...(routing.mode === "tasks" ? tasks : []),
            ].map((t) => (
              <label key={t.id} className="sf-dim block text-xs">
                {t.label}
                <select
                  aria-label={t.label}
                  className="sf-input mt-1"
                  value={
                    t.id === "single"
                      ? routing.single
                      : (routing.tasks[t.id as Task] ?? routing.single)
                  }
                  onChange={(e) =>
                    setRouting(
                      t.id === "single"
                        ? { ...routing, single: e.target.value }
                        : {
                            ...routing,
                            tasks: { ...routing.tasks, [t.id]: e.target.value },
                          },
                    )
                  }
                >
                  <option value="ollama">Ollama · local</option>
                  {connected.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.label} · {p.selection?.model} ·{" "}
                      {p.selection?.reasoning}
                    </option>
                  ))}
                </select>
              </label>
            ))}
            <p className="sf-mute text-xs">
              Code generation covers Write and generated code; repository
              reasoning covers review summaries. Routes take effect on
              subsequent model calls, not an already-running request. Stop an
              active run before switching. Embeddings remain on their existing
              provider to preserve your index.
            </p>
            <button
              className="sf-btn sf-btn-primary text-xs"
              disabled={busy}
              onClick={() =>
                void action("routing", "PUT", routing).then((data) => {
                  if (data)
                    setFeedback(
                      "Routing saved. Model failures fall back only to the configured local Ollama model.",
                    );
                })
              }
            >
              Save routing
            </button>
          </div>
        </>
      )}
      <div className="border-t sf-line pt-3">
        <p className="sf-dim text-xs">
          Local fallback: chat <code>{models?.chat_model ?? "checking"}</code> ·
          review <code>{models?.review_model ?? "checking"}</code>
        </p>
        {!!models?.models.length && (
          <label className="sf-dim mt-2 block text-xs">
            Local review fallback model
            <select
              aria-label="Local review fallback model"
              className="sf-input mt-1"
              disabled={busy}
              value={models.review_model}
              onChange={(e) =>
                void action(
                  "select",
                  "POST",
                  { review: e.target.value },
                  false,
                ).then((data) => {
                  if (data) {
                    window.dispatchEvent(new Event("savflux:models-changed"));
                    setFeedback("Local review fallback model updated.");
                  }
                })
              }
            >
              {!models.models.some((m) => m.name === models.review_model) && (
                <option value={models.review_model}>
                  {models.review_model} · configured
                </option>
              )}
              {models.models.map((m) => (
                <option key={m.name} value={m.name}>
                  {m.name}
                  {m.size_gb
                    ? ` · ${m.size_gb} GB download (runtime needs more RAM)`
                    : ""}
                </option>
              ))}
            </select>
          </label>
        )}
        <p className="sf-mute mt-1 text-xs">
          Installed models may still fail to load if RAM is insufficient. Start
          with a smaller model and a 1–3 file review.
        </p>
        <button
          className="sf-btn sf-btn-secondary mt-2 text-xs"
          disabled={busy}
          onClick={() => void probe("ollama")}
        >
          Test local review model
        </button>
      </div>
      <button
        className="sf-btn sf-btn-ghost text-xs"
        onClick={() =>
          window.dispatchEvent(new Event("savflux:models-changed"))
        }
      >
        Refresh runtime diagnostics
      </button>
      {models?.last_inference && (
        <p
          role="status"
          className="sf-dim rounded-lg border sf-line p-3 text-xs"
        >
          Last model call: {models.last_inference.provider} /{" "}
          {models.last_inference.model} ·{" "}
          {models.last_inference.ok ? "completed" : "failed"}
          {models.last_inference.fallback ? " · local fallback" : ""}.{" "}
          {models.last_inference.reason}
        </p>
      )}
      {busy && (
        <p className="sf-mute text-xs" role="status">
          Contacting model service…
        </p>
      )}
      {error && (
        <p role="alert" className="text-xs" style={{ color: "var(--sf-bad)" }}>
          {error}
        </p>
      )}
      {feedback && (
        <p role="status" className="sf-dim text-xs">
          {feedback}
        </p>
      )}
    </section>
  );
}
