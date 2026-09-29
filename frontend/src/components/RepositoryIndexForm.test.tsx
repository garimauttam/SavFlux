import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { RepositoryIndexForm } from "./RepositoryIndexForm";

const repo = "https://github.com/owner/project";
function response(branches = ["main", "feature/auth"], next_page: number | null = null) {
  return new Response(JSON.stringify({
    branches: branches.map((name) => ({ name, protected: name === "main" })), next_page,
  }));
}
function enterUrl(value = repo) {
  fireEvent.change(screen.getByLabelText("Public repository URL"), { target: { value } });
}
afterEach(() => vi.restoreAllMocks());

describe("repository remote branch selection", () => {
  it("loads remote refs and sends the selected slash branch when indexing", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(response());
    const onIndex = vi.fn().mockResolvedValue(true);
    render(<RepositoryIndexForm busy={false} onIndex={onIndex} />);
    expect(screen.getByRole("button", { name: "Index" })).toBeDisabled();
    expect(fetchSpy).not.toHaveBeenCalled();
    enterUrl();
    expect(screen.getByRole("status")).toHaveTextContent("Loading remote branches");
    await screen.findByRole("option", { name: "feature/auth" });
    expect(fetchSpy).toHaveBeenCalledWith(expect.stringContaining("/github/repos/owner/project/branches?page=1"), expect.anything());
    expect(screen.getByRole("option", { name: "main (protected)" })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Remote branch"), { target: { value: "feature/auth" } });
    fireEvent.click(screen.getByRole("button", { name: "Index" }));
    expect(onIndex).toHaveBeenCalledWith(repo, "feature/auth");
    await waitFor(() => expect(screen.getByLabelText("Public repository URL")).toHaveValue(""));
    expect(screen.queryByLabelText("Remote branch")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Index" })).toBeDisabled();
  });

  it("keeps repository default explicit and supports keyboard submission", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(response(["trunk"]));
    const onIndex = vi.fn().mockResolvedValue(true);
    render(<RepositoryIndexForm busy={false} onIndex={onIndex} />);
    enterUrl(`  ${repo}.git  `);
    await screen.findByRole("option", { name: "trunk" });
    expect(screen.getByLabelText("Remote branch")).toHaveValue("");
    fireEvent.submit(screen.getByRole("button", { name: "Index" }).closest("form")!);
    expect(onIndex).toHaveBeenCalledWith(`${repo}.git`, "");
    await waitFor(() => expect(screen.getByLabelText("Public repository URL")).toHaveValue(""));
  });

  it("clears selection and removes old options immediately when the URL changes", async () => {
    vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(response())
      .mockResolvedValueOnce(response(["develop"]));
    const onIndex = vi.fn().mockResolvedValue(true);
    render(<RepositoryIndexForm busy={false} onIndex={onIndex} />);
    enterUrl();
    await screen.findByRole("option", { name: "feature/auth" });
    fireEvent.change(screen.getByLabelText("Remote branch"), { target: { value: "feature/auth" } });
    enterUrl("https://github.com/other/repo");
    expect(screen.queryByRole("option", { name: "feature/auth" })).not.toBeInTheDocument();
    expect(screen.getByLabelText("Remote branch")).toHaveValue("");
    await screen.findByRole("option", { name: "develop" });
    fireEvent.click(screen.getByRole("button", { name: "Index" }));
    expect(onIndex).toHaveBeenCalledWith("https://github.com/other/repo", "");
    await waitFor(() => expect(screen.getByLabelText("Public repository URL")).toHaveValue(""));
  });

  it("ignores an obsolete response even if fetch does not honor abort", async () => {
    let resolveOld!: (value: Response) => void;
    const fetchSpy = vi.spyOn(globalThis, "fetch")
      .mockImplementationOnce(() => new Promise((resolve) => { resolveOld = resolve; }))
      .mockResolvedValueOnce(response(["new-branch"]));
    render(<RepositoryIndexForm busy={false} onIndex={vi.fn()} />);
    enterUrl();
    await waitFor(() => expect(fetchSpy).toHaveBeenCalledTimes(1));
    const signal = (fetchSpy.mock.calls[0][1] as RequestInit).signal;
    enterUrl("https://github.com/other/repo");
    expect(signal?.aborted).toBe(true);
    await screen.findByRole("option", { name: "new-branch" });
    await act(async () => { resolveOld(response(["old-branch"])); });
    expect(screen.queryByRole("option", { name: "old-branch" })).not.toBeInTheDocument();
  });

  it("loads additional pages without replacing a selected branch", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(response(["feature/first"], 2))
      .mockResolvedValueOnce(response(["feature/last"]));
    render(<RepositoryIndexForm busy={false} onIndex={vi.fn()} />);
    enterUrl();
    await screen.findByRole("option", { name: "feature/first" });
    fireEvent.change(screen.getByLabelText("Remote branch"), { target: { value: "feature/first" } });
    fireEvent.click(screen.getByRole("button", { name: "Load more branches" }));
    await screen.findByRole("option", { name: "feature/last" });
    expect(screen.getByLabelText("Remote branch")).toHaveValue("feature/first");
    expect(fetchSpy.mock.calls[1][0]).toContain("page=2");
    expect(screen.queryByRole("button", { name: "Load more branches" })).not.toBeInTheDocument();
  });

  it("shows lookup errors, allows default indexing, and offers retry", async () => {
    vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(new Response(JSON.stringify({ detail: "GitHub rate limit reached" }), { status: 429 }))
      .mockResolvedValueOnce(response());
    const onIndex = vi.fn().mockResolvedValue(false);
    render(<RepositoryIndexForm busy={false} onIndex={onIndex} />);
    enterUrl();
    expect(await screen.findByRole("alert")).toHaveTextContent("GitHub rate limit reached");
    fireEvent.click(screen.getByRole("button", { name: "Index" }));
    expect(onIndex).toHaveBeenCalledWith(repo, "");
    await waitFor(() => expect(screen.getByRole("button", { name: "Retry branches" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "Retry branches" }));
    await screen.findByRole("option", { name: "feature/auth" });
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("keeps the URL and branch on failure, then clears both after a successful retry", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () => response());
    const onIndex = vi.fn().mockResolvedValueOnce(false).mockResolvedValueOnce(true);
    render(<RepositoryIndexForm busy={false} onIndex={onIndex} />);
    enterUrl();
    await screen.findByRole("option", { name: "feature/auth" });
    fireEvent.change(screen.getByLabelText("Remote branch"), { target: { value: "feature/auth" } });
    fireEvent.click(screen.getByRole("button", { name: "Index" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Index" })).toBeEnabled());
    expect(screen.getByLabelText("Public repository URL")).toHaveValue(repo);
    expect(screen.getByLabelText("Remote branch")).toHaveValue("feature/auth");
    fireEvent.click(screen.getByRole("button", { name: "Index" }));
    await waitFor(() => expect(screen.getByLabelText("Public repository URL")).toHaveValue(""));
    expect(onIndex).toHaveBeenNthCalledWith(2, repo, "feature/auth");
    expect(screen.queryByLabelText("Remote branch")).not.toBeInTheDocument();
    enterUrl("https://github.com/owner/another");
    await screen.findByRole("option", { name: "feature/auth" });
    expect(screen.getByLabelText("Remote branch")).toHaveValue("");
  });

  it("keeps the inputs locked until indexing completes and prevents duplicate submits", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(response());
    let finish!: (success: boolean) => void;
    const onIndex = vi.fn(() => new Promise<boolean>((resolve) => { finish = resolve; }));
    render(<RepositoryIndexForm busy={false} onIndex={onIndex} />);
    enterUrl();
    await screen.findByRole("option", { name: "feature/auth" });
    fireEvent.click(screen.getByRole("button", { name: "Index" }));
    expect(screen.getByLabelText("Public repository URL")).toHaveValue(repo);
    expect(screen.getByLabelText("Public repository URL")).toBeDisabled();
    expect(screen.getByLabelText("Remote branch")).toBeDisabled();
    fireEvent.submit(screen.getByRole("button", { name: "Index" }).closest("form")!);
    expect(onIndex).toHaveBeenCalledTimes(1);
    await act(async () => { finish(true); });
    expect(screen.getByLabelText("Public repository URL")).toHaveValue("");
    expect(screen.getByLabelText("Public repository URL")).toBeEnabled();
  });

  it("reports empty repositories and locks controls during indexing", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(response([]));
    const onIndex = vi.fn();
    const { rerender } = render(<RepositoryIndexForm busy={false} onIndex={onIndex} />);
    enterUrl();
    await screen.findByText(/No remote branches found/);
    rerender(<RepositoryIndexForm busy onIndex={onIndex} />);
    expect(screen.getByLabelText("Remote branch")).toBeDisabled();
    expect(screen.getByLabelText("Public repository URL")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Index" })).toBeDisabled();
    fireEvent.submit(screen.getByRole("button", { name: "Index" }).closest("form")!);
    expect(onIndex).not.toHaveBeenCalled();
  });
});
