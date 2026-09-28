/**
 * SettingsDialog.tsx — model, retrieval and workspace preferences.
 *
 * WHAT BELONGS IN A SETTINGS DIALOG
 * ---------------------------------
 * Things that change how the product behaves but are not part of a task. The
 * old app had no such place: the model lived in a `.env` file, so "which model
 * is answering" was not a question the product could answer, let alone let you
 * change. That is the whole reason the free path felt broken — there was no way
 * to find out that Ollama was not running, or that the review model named in
 * `configs.json` was a 33B download nobody had.
 *
 * So the model is a setting you can *see*, with the reason it is unavailable
 * and the exact command that fixes it. Every value shown here is read from the
 * machine — the model list is Ollama's own `/api/tags`, not a hardcoded
 * catalogue that goes stale the moment someone pulls a new model.
 */

import { useEffect, useRef, useState } from "react";
import { Check, Cpu, Github, PanelRight, Server, X } from "lucide-react";
import type { ModelStatus } from "../../types/workspace";

interface SettingsDialogProps {
  open: boolean;
  onClose: () => void;
  models: ModelStatus | null;
  onSelectModel: (name: string) => void;
  onSelectMixtureModels: (names: string[]) => Promise<boolean>;
  onSelectProvider: (provider: ModelStatus["provider"], apiKey: string, model: string) => Promise<boolean>;
  onForgetProviderKey: (provider: Exclude<ModelStatus["provider"], "ollama">) => Promise<boolean>;
  providerBusy: boolean;
  providerError: string | null;
  onOpenGitHub: () => void;
  contextOpen: boolean;
  onToggleContext: () => void;
}

export function SettingsDialog({
  open,
  onClose,
  models,
  onSelectModel,
  onSelectMixtureModels,
  onSelectProvider,
  onForgetProviderKey,
  providerBusy,
  providerError,
  onOpenGitHub,
  contextOpen,
  onToggleContext,
}: SettingsDialogProps) {
  const [query, setQuery] = useState("");
  const [providerChoice, setProviderChoice] = useState<ModelStatus["provider"]>("ollama");
  const [providerApiKeyInput, setProviderApiKeyInput] = useState("");
  const [providerModelInput, setProviderModelInput] = useState("");
  const [providerFeedback, setProviderFeedback] = useState<string | null>(null);
  const [mixtureSelection, setMixtureSelection] = useState<string[]>([]);
  const [mixtureFeedback, setMixtureFeedback] = useState<string | null>(null);
  const [mixtureSaving, setMixtureSaving] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (open) {
      setQuery("");
      setProviderChoice(models?.provider ?? "ollama");
      setProviderApiKeyInput("");
      setProviderModelInput(models?.provider === "ollama" ? "" : models?.provider_model ?? "");
      setProviderFeedback(null);
      setMixtureFeedback(null);
    } else {
      setProviderApiKeyInput("");
    }
  }, [open]);

  useEffect(() => {
    if (open) setMixtureSelection(models?.summary_mixture_models ?? []);
  }, [open, models?.summary_mixture_models]);

  useEffect(() => {
    if (open && models) {
      setProviderChoice(models.provider);
      setProviderModelInput(models.provider === "ollama" ? "" : models.provider_model);
    }
  }, [open, models?.provider, models?.provider_model]);

  if (!open) return null;

  const filtered = (models?.models ?? []).filter((m) =>
    query ? m.name.toLowerCase().includes(query.toLowerCase()) : true,
  );

  const toggleMixtureModel = (name: string) => {
    setMixtureFeedback(null);
    setMixtureSelection((current) =>
      current.includes(name) ? current.filter((item) => item !== name) : [...current, name],
    );
  };

  const saveMixture = async () => {
    if (mixtureSelection.length === 1) {
      setMixtureFeedback("Choose at least two models, or clear the selection to turn MoA off.");
      return;
    }
    setMixtureSaving(true);
    setMixtureFeedback(null);
    try {
      const saved = await onSelectMixtureModels(mixtureSelection);
      setMixtureFeedback(saved
        ? mixtureSelection.length >= 2
          ? `MoA enabled with ${mixtureSelection.length} models for future repository summaries.`
          : "MoA disabled; repository summaries will use the configured model."
        : "Could not save this MoA selection. Check the model status and try again.");
    } finally {
      setMixtureSaving(false);
    }
  };

  const saveProvider = async () => {
    setProviderFeedback(null);
    const saved = await onSelectProvider(providerChoice, providerApiKeyInput, providerModelInput);
    if (saved) {
      setProviderApiKeyInput("");
      setProviderFeedback(
        providerChoice === "ollama"
          ? "Using the local Ollama model. No external provider key is sent."
          : `Using ${providerChoice} for model calls; Ollama is configured as the local fallback when available.`,
      );
    }
  };

  const forgetProviderKey = async () => {
    if (providerChoice === "ollama") return;
    const removed = await onForgetProviderKey(providerChoice);
    setProviderFeedback(removed
      ? `The saved ${providerChoice} key was removed. Environment configuration, if present, is unchanged.`
      : `No app-stored ${providerChoice} key was found.`);
  };

  return (
    <div
      className="fixed inset-0 z-[100] flex items-center justify-center p-4"
      style={{ background: "rgba(0,0,0,0.6)", backdropFilter: "blur(3px)" }}
      role="dialog"
      aria-modal="true"
      aria-labelledby="settings-title"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        ref={ref}
        className="max-h-[85vh] w-full max-w-[560px] overflow-hidden rounded-2xl border sf-line shadow-2xl shadow-black/60 sf-surface"
        onKeyDown={(e) => {
          if (e.key === "Escape") onClose();
        }}
      >
        <div className="flex items-center justify-between border-b sf-line px-5 py-3.5">
          <h2 id="settings-title" className="sf-text text-[15px] font-semibold">
            Settings
          </h2>
          <button type="button" onClick={onClose} aria-label="Close" className="sf-iconbtn h-7 w-7">
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="max-h-[62vh] overflow-y-auto px-5 py-4">
          {/* Model */}
          <section>
            <h3 className="sf-dim flex items-center gap-1.5 text-[12.5px] font-semibold">
              <Cpu className="h-3.5 w-3.5" /> Model
            </h3>
            <p className="sf-mute mt-1 text-[11.5px] leading-relaxed">
              {models?.provider_label ?? "Checking…"}
              {models?.free && " · runs on this machine · no key, no bill"}
            </p>

            <div className="mt-3 rounded-xl border sf-line bg-[var(--sf-raised)] p-3">
              <label className="sf-dim block text-[11px] font-medium" htmlFor="provider-choice">Chat and review provider</label>
              <select
                id="provider-choice"
                value={providerChoice}
                onChange={(event) => {
                  const next = event.target.value as ModelStatus["provider"];
                  setProviderChoice(next);
                  setProviderApiKeyInput("");
                  setProviderFeedback(null);
                  if (next !== models?.provider) setProviderModelInput("");
                }}
                disabled={providerBusy}
                className="sf-input mt-1.5"
              >
                <option value="ollama">Ollama · local and free</option>
                <option value="openai">OpenAI · use your API key</option>
                <option value="deepseek">DeepSeek · use your API key</option>
                <option value="openrouter">OpenRouter · choose a routed model</option>
              </select>

              {providerChoice !== "ollama" && (
                <>
                  <label className="sf-dim mt-3 block text-[11px] font-medium" htmlFor="provider-model">Model name</label>
                  <input
                    id="provider-model"
                    value={providerModelInput}
                    onChange={(event) => setProviderModelInput(event.target.value)}
                    placeholder={providerChoice === "deepseek" ? "deepseek-chat" : providerChoice === "openrouter" ? "openai/gpt-4o-mini" : "gpt-4o"}
                    autoComplete="off"
                    className="sf-input sf-mono mt-1.5"
                  />
                  <label className="sf-dim mt-3 block text-[11px] font-medium" htmlFor="provider-api-key">API key</label>
                  <input
                    id="provider-api-key"
                    type="password"
                    value={providerApiKeyInput}
                    onChange={(event) => setProviderApiKeyInput(event.target.value)}
                    placeholder={models?.provider_key_configured && providerChoice === models.provider
                      ? "Key configured — leave blank to keep it"
                      : "Paste a provider API key"}
                    autoComplete="new-password"
                    spellCheck={false}
                    className="sf-input sf-mono mt-1.5"
                  />
                  <p className="sf-mute mt-1.5 text-[10.5px] leading-relaxed">
                    The key is saved only on this SavFlux server and is never returned to the browser. Hosted providers receive code context in prompts. Ollama is the local fallback when available.
                  </p>
                </>
              )}

              {providerError && <p role="alert" className="mt-2 text-[11px] text-rose-300">{providerError}</p>}
              {providerFeedback && <p role="status" className="mt-2 text-[11px] sf-dim">{providerFeedback}</p>}
              <div className="mt-2.5 flex justify-end gap-2">
                {providerChoice !== "ollama" && models?.provider_key_source === "app" && providerChoice === models.provider && (
                  <button type="button" disabled={providerBusy} onClick={() => void forgetProviderKey()} className="sf-btn sf-btn-secondary px-2.5 py-1 text-[11px]">
                    Forget saved key
                  </button>
                )}
                <button type="button" disabled={providerBusy} onClick={() => void saveProvider()} className="sf-btn sf-btn-primary px-2.5 py-1 text-[11px]">
                  {providerBusy ? "Saving…" : "Save provider"}
                </button>
              </div>
            </div>

            {models && !models.available && (
              <div
                className="mt-2.5 rounded-lg border px-3 py-2.5 text-[11.5px]"
                style={{
                  borderColor: "rgba(251,191,36,0.28)",
                  background: "rgba(251,191,36,0.07)",
                  color: "#fcd34d",
                }}
              >
                <p>{models.hint}</p>
                {models.provider === "ollama" && (
                  <code className="sf-mono mt-1.5 block rounded border sf-line bg-black/20 px-2 py-1 text-[11px]">
                    {models.reachable ? `ollama pull ${models.chat_model}` : "ollama serve"}
                  </code>
                )}
              </div>
            )}

            {models && models.available && models.provider === "ollama" && (
              <>
                <p className="sf-dim mt-2.5 text-[12px]">
                  Chat: <span className="sf-mono">{models.chat_model}</span>
                </p>
                <p className="sf-dim text-[12px]">
                  Review: <span className="sf-mono">{models.review_model}</span>
                </p>
              </>
            )}
            {models && models.available && models.provider !== "ollama" && (
              <p className="sf-dim mt-2.5 text-[12px]">
                Provider model: <span className="sf-mono">{models.provider_model}</span>
                <span className="sf-mute"> · local fallback {models.local_fallback_available ? `available (${models.chat_model})` : "not installed or unreachable"}</span>
              </p>
            )}

            {(models?.models.length ?? 0) > 0 && (
              <div className="mt-3">
                <input
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  placeholder="Filter installed models…"
                  aria-label="Filter installed models"
                  className="sf-input"
                />
                <ul className="mt-2 max-h-48 space-y-1 overflow-y-auto">
                  {filtered.map((m) => {
                    const active = m.name === models?.chat_model;
                    return (
                      <li key={m.name}>
                        <button
                          type="button"
                          onClick={() => onSelectModel(m.name)}
                          className={[
                            "flex w-full items-center gap-2 rounded-lg border px-2.5 py-2 text-left transition-colors",
                            active ? "sf-accent-soft border-transparent" : "sf-line hover:bg-[var(--sf-raised)]",
                          ].join(" ")}
                        >
                          <Check className={`h-3.5 w-3.5 shrink-0 ${active ? "sf-accent" : "opacity-0"}`} />
                          <span className="min-w-0 flex-1">
                            <span className="sf-mono sf-text block truncate text-[12px]">{m.name}</span>
                            <span className="sf-mute block text-[11px]">
                              {[m.parameter_size, m.quantization, m.size_gb ? `${m.size_gb} GB` : null]
                                .filter(Boolean)
                                .join(" · ")}
                            </span>
                          </span>
                        </button>
                      </li>
                    );
                  })}
                </ul>
              </div>
            )}

            <section className="mt-4 rounded-xl border sf-line bg-[var(--sf-raised)] p-3">
              <div className="flex items-start justify-between gap-3">
                <div>
                  <h4 className="sf-text text-[12px] font-semibold">Mixture of Agents · repo summaries</h4>
                  <p className="sf-mute mt-1 text-[11px] leading-relaxed">
                    Run 2–8 installed Ollama models in parallel, then synthesize their drafts.
                    This adds latency and memory use; it only affects the overall review summary.
                  </p>
                </div>
                <span className="shrink-0 rounded-full border sf-line px-2 py-0.5 text-[10px] sf-dim">
                  {models?.summary_mixture_active ? "On" : "Off"}
                </span>
              </div>

              {(models?.models.length ?? 0) > 0 ? (
                <ul className="mt-2 space-y-1">
                  {models?.models.map((model) => {
                    const checked = mixtureSelection.includes(model.name);
                    const capped = !checked && mixtureSelection.length >= 8;
                    return (
                      <li key={`moa-${model.name}`}>
                        <label className={`flex items-center gap-2 rounded-md px-1.5 py-1 text-[11px] ${capped ? "opacity-50" : "cursor-pointer hover:bg-[var(--sf-overlay)]"}`}>
                          <input
                            type="checkbox"
                            checked={checked}
                            disabled={capped || mixtureSaving || providerBusy}
                            onChange={() => toggleMixtureModel(model.name)}
                            aria-label={`Include ${model.name} in Mixture of Agents`}
                            className="h-3.5 w-3.5 accent-[var(--sf-accent)]"
                          />
                          <span className="sf-mono sf-text truncate">{model.name}</span>
                          <span className="sf-mute ml-auto shrink-0">{model.size_gb ? `${model.size_gb} GB` : "installed"}</span>
                        </label>
                      </li>
                    );
                  })}
                </ul>
              ) : (
                <p className="sf-mute mt-2 rounded-lg border sf-line px-2.5 py-2 text-[11px]">
                  No installed Ollama models are available. Start Ollama and pull at least two models to configure MoA.
                </p>
              )}

              {models?.summary_mixture_missing_models.length ? (
                <p className="mt-2 text-[11px] text-amber-300">
                  Not installed: {models.summary_mixture_missing_models.join(", ")}. Pull these models or choose installed ones.
                </p>
              ) : null}
              {mixtureFeedback && (
                <p role="status" className={`mt-2 text-[11px] ${mixtureFeedback.startsWith("Could not") ? "text-rose-300" : "sf-dim"}`}>
                  {mixtureFeedback}
                </p>
              )}
              <div className="mt-2.5 flex items-center justify-between gap-2">
                <span className="sf-mute text-[10px]">
                  {mixtureSelection.length} selected · {mixtureSelection.length === 0 ? "empty disables MoA" : "select at least 2"}
                </span>
                <div className="flex items-center gap-2">
                  {mixtureSelection.length > 0 && (models?.models.length ?? 0) === 0 && (
                    <button
                      type="button"
                      onClick={() => setMixtureSelection([])}
                      className="sf-btn sf-btn-secondary px-2.5 py-1 text-[11px]"
                    >
                      Clear selection
                    </button>
                  )}
                  <button
                    type="button"
                    onClick={() => void saveMixture()}
                    disabled={mixtureSaving || providerBusy || mixtureSelection.length === 1}
                    className="sf-btn sf-btn-secondary px-2.5 py-1 text-[11px]"
                  >
                    {mixtureSaving ? "Saving…" : "Save MoA set"}
                  </button>
                </div>
              </div>
            </section>

            {models && (
              <p className="sf-mute mt-3 flex items-start gap-1.5 text-[11px] leading-relaxed">
                <Server className="mt-px h-3 w-3 shrink-0" />
                Retrieval embeds with{" "}
                <span className="sf-mono">{models.embedding_model}</span>
                {models.embedding_local ? " on this machine" : " through a hosted API"}. Changing it
                needs a re-index.
              </p>
            )}
          </section>

          {/* Workspace */}
          <section className="mt-6 border-t sf-line pt-4">
            <h3 className="sf-dim flex items-center gap-1.5 text-[12.5px] font-semibold">
              <PanelRight className="h-3.5 w-3.5" /> Workspace
            </h3>
            <label className="mt-2.5 flex cursor-pointer items-center gap-2.5">
              <input
                type="checkbox"
                checked={contextOpen}
                onChange={onToggleContext}
                className="h-3.5 w-3.5 accent-[var(--sf-accent)]"
              />
              <span className="sf-dim text-[12.5px]">
                Keep the context panel open
                <span className="sf-mute block text-[11px]">
                  Cited code appears next to the conversation instead of on another screen.
                </span>
              </span>
            </label>
          </section>

          {/* Integrations */}
          <section className="mt-6 border-t sf-line pt-4">
            <h3 className="sf-dim flex items-center gap-1.5 text-[12.5px] font-semibold">
              <Github className="h-3.5 w-3.5" /> GitHub
            </h3>
            <button type="button" onClick={onOpenGitHub} className="sf-btn sf-btn-secondary mt-2.5">
              <Github className="h-3.5 w-3.5" /> Manage the connection
            </button>
          </section>
        </div>
      </div>
    </div>
  );
}
