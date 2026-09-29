import { webcrypto } from "node:crypto";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import { apiFetch } from "../../api";
import { ReviewCodeWorkspace, reviewOutcome } from "./ReviewCodeWorkspace";
import type { ReviewSection } from "../../hooks/useMultiReview";
import type { ReviewFinding } from "../../lib/reviewFindings";
vi.mock("../../api", () => ({ apiFetch: vi.fn() }));
const code = "import requests\nrequests.get(url, verify=False)\n";
const finding: ReviewFinding = {
  ruleId: "tls",
  title: "Verify TLS",
  severity: "high",
  line: 2,
  message: "Disabling verification permits interception.",
  remediation: "Remove verify=False.",
  evidence: "requests.get(url, verify=False)",
  confidence: 1,
  origin: "static",
};
let file: ReviewSection;
beforeEach(async () => {
  vi.stubGlobal("crypto", webcrypto);
  const hash = await webcrypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(code),
  );
  file = {
    id: "https://github.com/o/r::src/auth.py",
    fileName: "auth.py",
    status: "complete",
    content: "A full model report.",
    tier: "full",
    findings: [finding],
    analysis: {
      contentHash: Buffer.from(hash).toString("hex"),
      parseError: "",
      findingCount: 1,
    },
  };
  vi.mocked(apiFetch).mockResolvedValue(
    new Response(JSON.stringify({ content: code })),
  );
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.mocked(apiFetch).mockReset();
});
const view = (sections = [file]) =>
  render(
    <ReviewCodeWorkspace
      sections={sections}
      active={false}
      currentStep={null}
      error={null}
      stopped={false}
    />,
  );

describe("Code review workspace", () => {
  it("anchors source-matched comments, with reasoning and suggestion, and resolves only in memory", async () => {
    view();
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Next comment" }),
      ).toBeEnabled(),
    );
    expect(
      screen.getByRole("article", { name: "Verify TLS at line 2" }),
    ).toHaveTextContent("Disabling verification permits interception.");
    expect(screen.getByText("Remove verify=False.")).toBeInTheDocument();
    expect(
      screen.queryByText("Comments without a verified line anchor"),
    ).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Mark reviewed" }));
    expect(screen.getByRole("button", { name: "Reopen" })).toBeInTheDocument();
    expect(apiFetch).toHaveBeenCalledTimes(1); // resolving does not save review data or edit code
  });
  it("does not attach comments to a changed source", async () => {
    vi.mocked(apiFetch).mockResolvedValue(
      new Response(JSON.stringify({ content: "changed code" })),
    );
    view();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "could not be matched",
    );
    expect(screen.getByRole("button", { name: "Next comment" })).toBeDisabled();
    expect(
      screen.getByText("Comments without a verified line anchor"),
    ).toBeInTheDocument();
  });
  it("preserves comments when hiding file and notes panels", async () => {
    view();
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Next comment" }),
      ).toBeEnabled(),
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Hide reviewed files" }),
    );
    expect(
      screen.queryByRole("complementary", { name: "Reviewed files" }),
    ).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Show review notes" }));
    expect(screen.getByText("A full model report.")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Hide review notes" }));
    expect(
      screen.getByRole("article", { name: "Verify TLS at line 2" }),
    ).toBeVisible();
  });
  it("filters files with findings without pretending other files were not reviewed", async () => {
    view([
      file,
      {
        id: "repo::src/clean.py",
        fileName: "clean.py",
        content: "",
        status: "pending",
      },
    ]);
    fireEvent.click(
      screen.getByRole("button", { name: "Only files with comments" }),
    );
    expect(screen.queryByRole("button", { name: /src\/clean.py/ })).toBeNull();
    expect(screen.getByText("1/2 files · 1 comment")).toBeInTheDocument();
    await screen.findByRole("article", { name: "Verify TLS at line 2" });
  });
  it("does not call a timeout a completed deep review", () => {
    expect(
      reviewOutcome({ ...file, fallbackReason: "model call timed out" }),
    ).toBe("Model unavailable · static only");
  });
  it("shows a recoverable error if source cannot be fetched", async () => {
    vi.mocked(apiFetch).mockResolvedValue(new Response("", { status: 404 }));
    view();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "no longer indexed",
    );
    expect(
      screen.getByRole("button", { name: "Retry loading code" }),
    ).toBeInTheDocument();
  });
});

it("shows files continuously, nested folders, colored source and the entire issue range", async () => {
  file.findings = [{ ...finding, line: 1, endLine: 2 }];
  vi.mocked(apiFetch).mockImplementation(
    async () => new Response(JSON.stringify({ content: code })),
  );
  const { container } = view([
    file,
    { ...file, id: "repo::lib/http.py", fileName: "http.py", findings: [] },
  ]);
  await waitFor(() =>
    expect(container.querySelectorAll(".sf-file-code").length).toBe(2),
  );
  expect(screen.getByRole("button", { name: "Folder src" })).toBeVisible();
  expect(screen.getByRole("button", { name: "Folder lib" })).toBeVisible();
  expect(container.querySelectorAll(".is-issue").length).toBe(2);
  expect(container.querySelectorAll(".token").length).toBeGreaterThan(0);
  fireEvent.click(screen.getByRole("button", { name: "Folder src" }));
  expect(screen.queryByRole("button", { name: /src\/auth.py:/ })).toBeNull();
  expect(screen.getByLabelText("Review of src/auth.py")).toBeInTheDocument();
});

it("does not eagerly fetch hundreds of off-screen files", async () => {
  const observe = vi.fn();
  vi.stubGlobal(
    "IntersectionObserver",
    class {
      observe = observe;
      disconnect = vi.fn();
    },
  );
  vi.mocked(apiFetch).mockImplementation(
    async () => new Response(JSON.stringify({ content: code })),
  );
  view(
    Array.from({ length: 288 }, (_, i) => ({
      ...file,
      id: `repo::src/file${i}.py`,
      fileName: `file${i}.py`,
    })),
  );
  await screen.findByRole("article", { name: "Verify TLS at line 2" });
  expect(apiFetch).toHaveBeenCalledTimes(1);
  expect(observe).toHaveBeenCalledTimes(287);
});

it("passes batch activity to fixes even on a completed file", async () => {
  const props = {
    sections: [file],
    currentStep: null,
    error: null,
    stopped: false,
  };
  const { rerender } = render(<ReviewCodeWorkspace {...props} active />);
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "Next comment" })).toBeEnabled(),
  );
  fireEvent.click(screen.getByRole("button", { name: /Suggested fix/ }));
  expect(
    screen.getByRole("button", { name: "Generate code fix" }),
  ).toBeDisabled();
  expect(apiFetch).toHaveBeenCalledTimes(1); // source fetch only
  rerender(<ReviewCodeWorkspace {...props} active={false} stopped />);
  expect(
    screen.getByRole("button", { name: "Generate code fix" }),
  ).toBeEnabled();
});
