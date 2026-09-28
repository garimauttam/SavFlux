import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { SettingsDialog } from "./SettingsDialog";
import type { ModelStatus } from "../../types/workspace";

const models: ModelStatus = {
  provider: "ollama",
  provider_label: "Local · Ollama",
  provider_model: "qwen2.5-coder:7b",
  provider_source: "env",
  provider_key_configured: false,
  provider_key_source: null,
  local_fallback_enabled: false,
  local_fallback_available: true,
  free: true,
  base_url: "http://localhost:11434",
  available: true,
  reachable: true,
  kind: null,
  hint: null,
  chat_model: "qwen2.5-coder:7b",
  review_model: "qwen2.5-coder:7b",
  summary_mixture_models: [],
  summary_mixture_active: false,
  summary_mixture_missing_models: [],
  summary_mixture_source: "app",
  embedding_model: "sentence-transformers/all-MiniLM-L6-v2",
  embedding_local: true,
  models: [
    { name: "qwen2.5-coder:7b", size_gb: 4.7 },
    { name: "deepseek-r1:7b", size_gb: 4.7 },
  ],
  suggested: [],
  selection_source: "env",
};

function renderDialog(
  onSelectMixtureModels = vi.fn().mockResolvedValue(true),
  onSelectProvider = vi.fn().mockResolvedValue(true),
) {
  return {
    onSelectMixtureModels,
    onSelectProvider,
    ...render(
      <SettingsDialog
        open
        onClose={vi.fn()}
        models={models}
        onSelectModel={vi.fn()}
        onSelectMixtureModels={onSelectMixtureModels}
        onSelectProvider={onSelectProvider}
        onForgetProviderKey={vi.fn().mockResolvedValue(true)}
        providerBusy={false}
        providerError={null}
        onOpenGitHub={vi.fn()}
        contextOpen={false}
        onToggleContext={vi.fn()}
      />,
    ),
  };
}

describe("SettingsDialog · MoA model selection", () => {
  it("saves the installed models selected for repo-summary synthesis", async () => {
    const save = vi.fn().mockResolvedValue(true);
    renderDialog(save);

    fireEvent.click(screen.getByRole("checkbox", { name: "Include qwen2.5-coder:7b in Mixture of Agents" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Include deepseek-r1:7b in Mixture of Agents" }));
    fireEvent.click(screen.getByRole("button", { name: "Save MoA set" }));

    await waitFor(() => expect(save).toHaveBeenCalledWith(["qwen2.5-coder:7b", "deepseek-r1:7b"]));
    expect(await screen.findByRole("status")).toHaveTextContent("MoA enabled with 2 models");
  });

  it("does not allow saving a one-model set", () => {
    renderDialog();
    fireEvent.click(screen.getByRole("checkbox", { name: "Include qwen2.5-coder:7b in Mixture of Agents" }));
    expect(screen.getByRole("button", { name: "Save MoA set" })).toBeDisabled();
  });

  it("sends a provider key only when the user submits the settings form", async () => {
    const saveProvider = vi.fn().mockResolvedValue(true);
    renderDialog(vi.fn().mockResolvedValue(true), saveProvider);

    fireEvent.change(screen.getByLabelText("Chat and review provider"), { target: { value: "openai" } });
    fireEvent.change(screen.getByLabelText("Model name"), { target: { value: "gpt-4o-mini" } });
    fireEvent.change(screen.getByLabelText("API key"), { target: { value: "sk-test-user-key" } });
    fireEvent.click(screen.getByRole("button", { name: "Save provider" }));

    await waitFor(() => expect(saveProvider).toHaveBeenCalledWith("openai", "sk-test-user-key", "gpt-4o-mini"));
    expect(await screen.findByRole("status")).toHaveTextContent("Ollama is configured as the local fallback");
  });
});
