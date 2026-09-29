import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { apiFetch } from "../../api";
import { ModelConnections } from "./ModelConnections";
vi.mock("../../api", () => ({ apiFetch: vi.fn() }));
const model = {
  id: "openai/gpt-oss-20b",
  name: "GPT OSS",
  reasoning: ["low", "medium", "high"],
  cost: "free account",
};
const initial = {
  providers: [
    {
      id: "groq",
      label: "Groq",
      key_configured: false,
      note: "Free plan quotas",
      key_url: "https://console.groq.com/keys",
      limits_url: "https://console.groq.com/docs/rate-limits",
    },
    {
      id: "openrouter",
      label: "OpenRouter",
      key_configured: true,
      selection: {
        model: "vendor/code:free",
        reasoning: "default",
        reasoning_options: [],
      },
    },
  ],
  routing: { mode: "single", single: "ollama", tasks: {} },
  managed: false,
};
const json = (data: unknown, status = 200) =>
  new Response(JSON.stringify(data), { status });
beforeEach(() => {
  vi.mocked(apiFetch).mockImplementation(async (url) =>
    url.endsWith("/catalog") ? json({ models: [model] }) : json(initial),
  );
});
afterEach(() => vi.clearAllMocks());
it("does not submit typed keys until explicit action, and requires consent before storage", async () => {
  render(<ModelConnections models={null} />);
  await screen.findByRole("button", { name: /Groq/ });
  fireEvent.change(screen.getByLabelText("Provider API key"), {
    target: { value: "secret-never-in-storage" },
  });
  expect(apiFetch).toHaveBeenCalledTimes(1);
  fireEvent.click(
    screen.getByRole("button", { name: "Load / refresh models" }),
  );
  await screen.findByRole("option", { name: model.id });
  fireEvent.change(screen.getByLabelText("Cloud model"), {
    target: { value: model.id },
  });
  fireEvent.change(screen.getByLabelText("Reasoning effort"), {
    target: { value: "low" },
  });
  expect(
    screen.getByRole("button", { name: "Save connection" }),
  ).toBeDisabled();
  fireEvent.click(screen.getByRole("checkbox", { name: /I consent/ }));
  fireEvent.click(screen.getByRole("checkbox", { name: /I checked/ }));
  fireEvent.click(screen.getByRole("button", { name: "Save connection" }));
  await waitFor(() =>
    expect(apiFetch).toHaveBeenCalledWith(
      "/api/v1/models/connections",
      expect.objectContaining({
        method: "PUT",
        body: expect.stringContaining('"reasoning":"low"'),
      }),
    ),
  );
  await waitFor(() =>
    expect(screen.getByLabelText("Provider API key")).toHaveValue(""),
  );
  expect(localStorage.length).toBe(0);
});
it("clears the key and resets reasoning when switching providers", async () => {
  render(<ModelConnections models={null} />);
  await screen.findByRole("button", { name: /Groq/ });
  fireEvent.change(screen.getByLabelText("Provider API key"), {
    target: { value: "secret" },
  });
  fireEvent.click(screen.getByRole("button", { name: /OpenRouter/ }));
  expect(screen.getByLabelText("Provider API key")).toHaveValue("");
  expect(screen.getByLabelText("Reasoning effort")).toBeDisabled();
  expect(screen.getByLabelText("Cloud model")).toHaveValue("vendor/code:free");
});
it("saves explicit task routes without invoking any model", async () => {
  render(<ModelConnections models={null} />);
  await screen.findByRole("button", { name: /Groq/ });
  fireEvent.change(screen.getByLabelText("Routing mode"), {
    target: { value: "tasks" },
  });
  fireEvent.change(screen.getByLabelText("Code generation"), {
    target: { value: "openrouter" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Save routing" }));
  await waitFor(() =>
    expect(apiFetch).toHaveBeenCalledWith(
      "/api/v1/models/routing",
      expect.objectContaining({
        method: "PUT",
        body: JSON.stringify({
          mode: "tasks",
          single: "ollama",
          tasks: { coding: "openrouter" },
        }),
      }),
    ),
  );
  expect(
    vi.mocked(apiFetch).mock.calls.some(([path]) => path.endsWith("/test")),
  ).toBe(false);
});
it("reports a catalog failure without storing or dropping the user's key", async () => {
  vi.mocked(apiFetch).mockImplementation(async (path) =>
    path.endsWith("/catalog")
      ? json({ detail: "Authentication denied" }, 502)
      : json(initial),
  );
  render(<ModelConnections models={null} />);
  await screen.findByRole("button", { name: /Groq/ });
  fireEvent.change(screen.getByLabelText("Provider API key"), {
    target: { value: "private-key" },
  });
  fireEvent.click(
    screen.getByRole("button", { name: "Load / refresh models" }),
  );
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "Authentication denied",
  );
  expect(screen.getByLabelText("Provider API key")).toHaveValue("private-key");
});
