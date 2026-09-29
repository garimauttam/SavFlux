import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { BranchComparePanel } from "./BranchComparePanel";

const branches = [
  { name: "main", protected: true, sha: "a1b2c3d4" },
  { name: "feature/auth", protected: false, sha: "d4c3b2a1" },
];

function renderPanel(connected = true) {
  return render(
    <BranchComparePanel
      activeRepoUrl="https://github.com/acme/widget"
      selectedBranch="feature/auth"
      defaultBranch="main"
      branches={branches}
      branchesLoading={false}
      githubConnected={connected}
      onConnectGitHub={vi.fn()}
    />,
  );
}

beforeEach(() => vi.restoreAllMocks());

describe("BranchComparePanel", () => {
  it("compares the selected remote refs and renders the GitHub diff summary", async () => {
    const fetchMock = vi.fn(
      async (_input: RequestInfo | URL, init?: RequestInit) => {
        expect(init?.method).toBe("POST");
        expect(JSON.parse(String(init?.body))).toEqual({
          repo: "acme/widget",
          base: "main",
          head: "feature/auth",
        });
        return new Response(
          JSON.stringify({
            repo: "acme/widget",
            base: "main",
            head: "feature/auth",
            status: "ahead",
            ahead_by: 2,
            behind_by: 0,
            total_commits: 2,
            changed_files: 1,
            files: [
              {
                filename: "src/auth.ts",
                status: "modified",
                additions: 2,
                deletions: 1,
                changes: 3,
                patch: "@@ -1 +1 @@\n-old\n+new",
                patch_truncated: false,
              },
            ],
            files_truncated: false,
            commits: [
              {
                sha: "12345678",
                message: "Harden auth",
                author: "A Dev",
                date: "2026-09-01T00:00:00Z",
              },
            ],
            commits_truncated: false,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      },
    );
    vi.stubGlobal("fetch", fetchMock);
    renderPanel(false);

    await waitFor(() =>
      expect(screen.getByLabelText("Base branch")).toHaveValue("main"),
    );
    expect(screen.getByLabelText("Compare branch")).toHaveValue("feature/auth");
    expect(screen.getByRole("button", { name: "Compare" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "Compare" }));

    expect(
      await screen.findByText("Compare branch is ahead"),
    ).toBeInTheDocument();
    expect(screen.getByText("2 ahead")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /src\/auth\.ts/ }));
    expect(screen.getByText("+new")).toBeInTheDocument();
    expect(screen.getByText("Harden auth")).toBeInTheDocument();
  });

  it("surfaces GitHub errors instead of an empty comparison", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(
            JSON.stringify({
              detail: "Not found — the repository does not exist.",
            }),
            { status: 404, headers: { "Content-Type": "application/json" } },
          ),
      ),
    );
    renderPanel();
    await waitFor(() =>
      expect(screen.getByLabelText("Compare branch")).toHaveValue(
        "feature/auth",
      ),
    );
    fireEvent.click(screen.getByRole("button", { name: "Compare" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Not found");
    expect(
      screen.queryByText("No file changes between these refs."),
    ).toBeNull();
  });

  it("asks for a repository rather than showing a misleading blank diff", () => {
    render(
      <BranchComparePanel
        activeRepoUrl={null}
        selectedBranch=""
        defaultBranch=""
        branches={[]}
        branchesLoading={false}
        githubConnected={false}
        onConnectGitHub={vi.fn()}
      />,
    );
    expect(screen.getByText("Compare repository branches")).toBeInTheDocument();
  });
});

it("explains same-ref and loading states instead of silently disabling Compare", async () => {
  renderPanel(false);
  await waitFor(() =>
    expect(screen.getByLabelText("Base branch")).toHaveValue("main"),
  );
  fireEvent.change(screen.getByLabelText("Compare branch"), {
    target: { value: "main" },
  });
  expect(screen.getByRole("button", { name: "Compare" })).toBeDisabled();
  expect(
    screen.getByRole("button", { name: "Compare" }),
  ).toHaveAccessibleDescription("Choose two different branches to compare.");
});
it("aborts an old comparison when refs change and ignores late output", async () => {
  let finish!: (response: Response) => void;
  const fetchMock = vi.fn(
    () =>
      new Promise<Response>((resolve) => {
        finish = resolve;
      }),
  );
  vi.stubGlobal("fetch", fetchMock);
  renderPanel(false);
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "Compare" })).toBeEnabled(),
  );
  fireEvent.click(screen.getByRole("button", { name: "Compare" }));
  await waitFor(() => expect(fetchMock).toHaveBeenCalled());
  const signal = (
    fetchMock.mock.calls[0] as unknown as [unknown, RequestInit]
  )[1].signal;
  fireEvent.change(screen.getByLabelText("Compare branch"), {
    target: { value: "main" },
  });
  expect(signal?.aborted).toBe(true);
  await act(async () => {
    finish(
      new Response(
        JSON.stringify({
          status: "ahead",
          ahead_by: 1,
          behind_by: 0,
          files: [],
          commits: [],
        }),
      ),
    );
  });
  expect(screen.queryByText("Compare branch is ahead")).toBeNull();
});
it("retains manual refs when branch data refreshes and resets for a new repository", async () => {
  const props = {
    activeRepoUrl: "https://github.com/o/r",
    selectedBranch: "feature/auth",
    defaultBranch: "main",
    branches,
    branchesLoading: false,
    githubConnected: false,
    onConnectGitHub: vi.fn(),
  };
  const { rerender } = render(<BranchComparePanel {...props} />);
  await waitFor(() =>
    expect(screen.getByLabelText("Base branch")).toHaveValue("main"),
  );
  fireEvent.change(screen.getByLabelText("Base branch"), {
    target: { value: "feature/auth" },
  });
  fireEvent.change(screen.getByLabelText("Compare branch"), {
    target: { value: "main" },
  });
  rerender(<BranchComparePanel {...props} branches={[...branches]} />);
  expect(screen.getByLabelText("Base branch")).toHaveValue("feature/auth");
  expect(screen.getByLabelText("Compare branch")).toHaveValue("main");
  rerender(
    <BranchComparePanel
      {...props}
      activeRepoUrl="https://github.com/o/other"
    />,
  );
  await waitFor(() =>
    expect(screen.getByLabelText("Base branch")).toHaveValue("main"),
  );
  expect(screen.getByLabelText("Compare branch")).toHaveValue("feature/auth");
});
it.each([404, 429])(
  "gives public-read failure guidance for HTTP %s",
  async (status) => {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(
            JSON.stringify({ detail: "GitHub refused comparison" }),
            { status },
          ),
      ),
    );
    renderPanel(false);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Compare" })).toBeEnabled(),
    );
    fireEvent.click(screen.getByRole("button", { name: "Compare" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      status === 404 ? "private repository" : "API limit",
    );
    expect(screen.getByRole("button", { name: "Compare" })).toBeEnabled();
  },
);

it("can compare loaded public refs even if default-branch metadata is unavailable", async () => {
  const props = {
    activeRepoUrl: "https://github.com/o/r",
    selectedBranch: "feature/auth",
    defaultBranch: "",
    branches,
    branchesLoading: false,
    githubConnected: false,
    onConnectGitHub: vi.fn(),
  };
  const { rerender } = render(<BranchComparePanel {...props} />);
  await waitFor(() =>
    expect(screen.getByLabelText("Base branch")).toHaveValue("main"),
  );
  expect(screen.getByRole("button", { name: "Compare" })).toBeEnabled();
  rerender(<BranchComparePanel {...props} branchesLoading />);
  expect(screen.getByRole("button", { name: "Compare" })).toBeDisabled();
  expect(
    screen.getByRole("button", { name: "Compare" }),
  ).toHaveAccessibleDescription("Loading branches from GitHub…");
});
