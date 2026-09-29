import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { apiFetch } from "../api";
import DiffViewer from "./DiffViewer";
vi.mock("../api", () => ({ apiFetch: vi.fn() }));
const repo = "https://github.com/o/r";
const files = ["src/one.ts", "src/two.ts", "src/three.ts"].map((path) => ({
  source: `${repo}::${path}`,
  file_name: path,
  language: "typescript",
  repo_url: repo,
}));
const data = {
  unified_diff: "--- one\n+++ two\n@@ -1 +1 @@\n-old\n+new",
  a_content: "old",
  b_content: "new",
  added: 1,
  removed: 1,
  similarity: 0.5,
  a_lines: 1,
  b_lines: 1,
};
const reply = (data: unknown, status = 200) =>
  new Response(JSON.stringify(data), { status });
beforeEach(() => {
  vi.mocked(apiFetch).mockReset();
  vi.mocked(apiFetch).mockImplementation(async (url) =>
    String(url).includes("indexed-files")
      ? reply({
          files: [
            ...files,
            {
              source: "https://github.com/other/r::file.ts",
              repo_url: "https://github.com/other/r",
              file_name: "file.ts",
            },
          ],
        })
      : reply(data),
  );
});
afterEach(() => vi.restoreAllMocks());
async function choose() {
  await waitFor(() => expect(screen.getByLabelText("File A")).toBeEnabled());
  fireEvent.change(screen.getByLabelText("File A"), {
    target: { value: files[0].source },
  });
  fireEvent.change(screen.getByLabelText("File B"), {
    target: { value: files[1].source },
  });
}
it("scopes files to the repository and requires explicit, distinct selections", async () => {
  render(<DiffViewer activeRepoUrl={repo} />);
  await waitFor(() => expect(screen.getByLabelText("File A")).toBeEnabled());
  expect(screen.getByRole("button", { name: "Compare" })).toBeDisabled();
  expect(screen.queryByRole("option", { name: "file.ts" })).toBeNull();
  await choose();
  expect(screen.getByRole("button", { name: "Compare" })).toBeEnabled();
  fireEvent.change(screen.getByLabelText("File B"), {
    target: { value: files[0].source },
  });
  expect(
    screen.getByRole("button", { name: "Compare" }),
  ).toHaveAccessibleDescription("Choose two different files to compare.");
});
it("keeps selected options during search and compares/copies the requested snapshots", async () => {
  const writeText = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", {
    value: { writeText },
    configurable: true,
  });
  render(<DiffViewer activeRepoUrl={repo} />);
  await choose();
  fireEvent.change(screen.getByLabelText("Filter files"), {
    target: { value: "three" },
  });
  expect(screen.getByLabelText("File A")).toHaveValue(files[0].source);
  expect(
    within(screen.getByLabelText("File B")).getByRole("option", {
      name: "src/two.ts",
    }),
  ).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Compare" }));
  await screen.findByLabelText("Unified file diff");
  expect(
    JSON.parse(String(vi.mocked(apiFetch).mock.calls.at(-1)?.[1]?.body)),
  ).toEqual({
    source_a: files[0].source,
    source_b: files[1].source,
    context: 3,
  });
  fireEvent.click(screen.getByRole("button", { name: "Copy diff" }));
  await screen.findByText("Diff copied.");
  expect(writeText).toHaveBeenCalledWith(data.unified_diff);
  fireEvent.click(screen.getByRole("button", { name: "Swap files" }));
  expect(screen.queryByLabelText("Indexed comparison result")).toBeNull();
  expect(screen.getByLabelText("File A")).toHaveValue(files[1].source);
});
it("cancels stale requests when selectors change and ignores their late output", async () => {
  render(<DiffViewer activeRepoUrl={repo} />);
  await choose();
  let finish!: (response: Response) => void;
  vi.mocked(apiFetch).mockImplementationOnce(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  );
  fireEvent.click(screen.getByRole("button", { name: "Compare" }));
  const signal = vi.mocked(apiFetch).mock.calls.at(-1)?.[1]?.signal;
  fireEvent.change(screen.getByLabelText("Diff context"), {
    target: { value: "5" },
  });
  expect(signal?.aborted).toBe(true);
  await act(async () => {
    finish(reply(data));
  });
  expect(screen.queryByLabelText("Indexed comparison result")).toBeNull();
});
it("distinguishes identical contents from not compared, and supports empty-file previews", async () => {
  render(<DiffViewer activeRepoUrl={repo} />);
  await choose();
  vi.mocked(apiFetch).mockResolvedValueOnce(
    reply({
      ...data,
      unified_diff: "",
      a_content: "",
      b_content: "",
      a_lines: 0,
      b_lines: 0,
      added: 0,
      removed: 0,
      similarity: 1,
    }),
  );
  fireEvent.click(screen.getByRole("button", { name: "Compare" }));
  await screen.findByText("These indexed files have identical contents.");
  fireEvent.click(
    screen.getByRole("button", { name: "Side-by-side previews" }),
  );
  expect(screen.getAllByText("Empty file")).toHaveLength(2);
  expect(screen.getByRole("button", { name: "Copy diff" })).toBeDisabled();
});
it("surfaces file-list errors and offers retry rather than silently rendering an empty index", async () => {
  vi.mocked(apiFetch).mockResolvedValueOnce(reply({}, 503));
  render(<DiffViewer activeRepoUrl={repo} />);
  expect(await screen.findByRole("alert")).toHaveTextContent("HTTP 503");
  fireEvent.click(screen.getByRole("button", { name: "Retry loading files" }));
  await waitFor(() => expect(screen.getByLabelText("File A")).toBeEnabled());
});

it("marks truncated previews rather than labeling them complete files", async () => {
  render(<DiffViewer activeRepoUrl={repo} />);
  await choose();
  vi.mocked(apiFetch).mockResolvedValueOnce(
    reply({ ...data, a_truncated: true, b_truncated: false }),
  );
  fireEvent.click(screen.getByRole("button", { name: "Compare" }));
  await screen.findByLabelText("Unified file diff");
  fireEvent.click(
    screen.getByRole("button", { name: "Side-by-side previews" }),
  );
  expect(
    within(screen.getByLabelText("File A snapshot")).getByText(
      /Preview limited/,
    ),
  ).toBeVisible();
  expect(
    within(screen.getByLabelText("File B snapshot")).queryByText(
      /Preview limited/,
    ),
  ).toBeNull();
});
