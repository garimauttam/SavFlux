import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { apiFetch } from "../api";
import { AgentWorkspacePanel } from "./AgentWorkspacePanel";

vi.mock("../api", () => ({ apiFetch: vi.fn() }));
const fetchMock = vi.mocked(apiFetch);

afterEach(() => {
  fetchMock.mockReset();
  vi.restoreAllMocks();
});

const response = (body: unknown) => new Response(JSON.stringify(body), { status: 200 });

describe("AgentWorkspacePanel", () => {
  it("explains that no persistent workspace exists before an Agent patch", () => {
    render(<AgentWorkspacePanel repoUrl={null} />);
    expect(screen.getByText("Choose an indexed GitHub repository")).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("shows the local diff, branch and safe reset action", async () => {
    fetchMock.mockResolvedValue(response({
      exists: true,
      dirty: true,
      branch: "savflux/agent-123",
      base_sha: "abcdef1234567890",
      changed_file_count: 1,
      files_changed: [{ status: "M", path: "src/module.py" }],
      stat: "1 file changed, 1 insertion(+), 1 deletion(-)",
      diff: "-value = 1\n+value = 2\n",
      truncated: false,
    }));
    vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<AgentWorkspacePanel repoUrl="https://github.com/acme/widgets" />);

    expect(await screen.findByText("savflux/agent-123")).toBeInTheDocument();
    expect(screen.getByText("src/module.py")).toBeInTheDocument();
    expect(screen.getByText(/local Git worktree on the SavFlux server/)).toBeInTheDocument();

    fetchMock.mockResolvedValue(response({ exists: true, dirty: false, reset: true }));
    fireEvent.click(screen.getByRole("button", { name: /Reset edits/ }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(String(fetchMock.mock.calls[1][0])).toContain("/api/v1/workspace/reset");
    expect(fetchMock.mock.calls[1][1]?.method).toBe("POST");
  });

  it("offers a reset for a clean but stale worktree so it can advance to the new index", async () => {
    fetchMock.mockResolvedValue(response({
      exists: true,
      dirty: false,
      stale_index: true,
      base_sha: "1111111111",
      indexed_sha: "2222222222",
      files_changed: [],
    }));
    vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<AgentWorkspacePanel repoUrl="https://github.com/acme/widgets" />);

    expect(await screen.findByText(/re-indexed since this worktree was created/)).toBeInTheDocument();
    const resetButton = screen.getByRole("button", { name: /Update base/ });
    expect(resetButton).toBeInTheDocument();
    fetchMock.mockResolvedValue(response({ exists: true, dirty: false, stale_index: false }));
    fireEvent.click(resetButton);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(String(fetchMock.mock.calls[1][0])).toContain("/api/v1/workspace/reset");
  });
});
