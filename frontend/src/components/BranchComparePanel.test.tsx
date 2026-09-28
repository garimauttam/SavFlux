import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { BranchComparePanel } from "./BranchComparePanel";

const branches = [
  { name: "main", protected: true, sha: "a1b2c3d4" },
  { name: "feature/auth", protected: false, sha: "d4c3b2a1" },
];

function renderPanel() {
  return render(
    <BranchComparePanel
      activeRepoUrl="https://github.com/acme/widget"
      selectedBranch="feature/auth"
      defaultBranch="main"
      branches={branches}
      branchesLoading={false}
      githubConnected
      onConnectGitHub={vi.fn()}
    />,
  );
}

beforeEach(() => vi.restoreAllMocks());

describe("BranchComparePanel", () => {
  it("compares the selected remote refs and renders the GitHub diff summary", async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      expect(init?.method).toBe("POST");
      expect(JSON.parse(String(init?.body))).toEqual({ repo: "acme/widget", base: "main", head: "feature/auth" });
      return new Response(JSON.stringify({
        repo: "acme/widget",
        base: "main",
        head: "feature/auth",
        status: "ahead",
        ahead_by: 2,
        behind_by: 0,
        total_commits: 2,
        changed_files: 1,
        files: [{
          filename: "src/auth.ts",
          status: "modified",
          additions: 2,
          deletions: 1,
          changes: 3,
          patch: "@@ -1 +1 @@\n-old\n+new",
          patch_truncated: false,
        }],
        files_truncated: false,
        commits: [{ sha: "12345678", message: "Harden auth", author: "A Dev", date: "2026-09-01T00:00:00Z" }],
        commits_truncated: false,
      }), { status: 200, headers: { "Content-Type": "application/json" } });
    });
    vi.stubGlobal("fetch", fetchMock);
    renderPanel();

    await waitFor(() => expect(screen.getByLabelText("Base branch")).toHaveValue("main"));
    expect(screen.getByLabelText("Compare branch")).toHaveValue("feature/auth");
    fireEvent.click(screen.getByRole("button", { name: "Compare" }));

    expect(await screen.findByText("Compare branch is ahead")).toBeInTheDocument();
    expect(screen.getByText("2 ahead")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /src\/auth\.ts/ }));
    expect(screen.getByText("+new")).toBeInTheDocument();
    expect(screen.getByText("Harden auth")).toBeInTheDocument();
  });

  it("surfaces GitHub errors instead of an empty comparison", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(
      JSON.stringify({ detail: "Not found — the repository does not exist." }),
      { status: 404, headers: { "Content-Type": "application/json" } },
    )));
    renderPanel();
    await waitFor(() => expect(screen.getByLabelText("Compare branch")).toHaveValue("feature/auth"));
    fireEvent.click(screen.getByRole("button", { name: "Compare" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Not found");
    expect(screen.queryByText("No file changes between these refs.")).toBeNull();
  });

  it("asks for a repository rather than showing a misleading blank diff", () => {
    render(<BranchComparePanel
      activeRepoUrl={null}
      selectedBranch=""
      defaultBranch=""
      branches={[]}
      branchesLoading={false}
      githubConnected={false}
      onConnectGitHub={vi.fn()}
    />);
    expect(screen.getByText("Compare repository branches")).toBeInTheDocument();
  });
});
