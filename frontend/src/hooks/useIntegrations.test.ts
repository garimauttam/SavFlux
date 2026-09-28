/**
 * useRepoWriteAccess.test.ts — three states, and only one of them removes a
 * control.
 *
 * The hook exists so that a read-only GitHub token never offers a push that
 * would fail. The easy mistake is to treat "I don't know yet" as "no", which
 * makes the Create PR button flicker out on every page load and permanently
 * disappear for anyone not connected to GitHub — including the people for whom
 * the `gh` CLI path still works perfectly well with no token at all.
 *
 * So: `false` is the only value that removes functionality, and it is only ever
 * reached from a real answer from GitHub.
 */
import { afterEach, describe, expect, it, vi, type Mock } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { apiFetch } from "../api";
import { useRepoWriteAccess } from "./useIntegrations";

vi.mock("../api", () => ({ apiFetch: vi.fn() }));
const fetchMock = apiFetch as unknown as Mock;

afterEach(() => {
  fetchMock.mockReset();
});

const repo = (push: boolean) => ({
  full_name: "o/r",
  permissions: { admin: false, push, pull: true },
});

describe("useRepoWriteAccess", () => {
  it("reports false when GitHub says the token cannot push", async () => {
    fetchMock.mockResolvedValue({ ok: true, json: async () => repo(false) });
    const { result } = renderHook(() => useRepoWriteAccess("o/r"));
    await waitFor(() => expect(result.current.canWrite).toBe(false));
    expect(result.current.loading).toBe(false);
  });

  it("reports true when GitHub says it can", async () => {
    fetchMock.mockResolvedValue({ ok: true, json: async () => repo(true) });
    const { result } = renderHook(() => useRepoWriteAccess("o/r"));
    await waitFor(() => expect(result.current.canWrite).toBe(true));
  });

  it("asks GitHub, and asks about the repository it was given", async () => {
    fetchMock.mockResolvedValue({ ok: true, json: async () => repo(true) });
    renderHook(() => useRepoWriteAccess("pallets/click"));
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(fetchMock.mock.calls[0][0]).toContain("pallets/click");
  });

  it("stays unknown — not false — when GitHub is not connected", async () => {
    /**
     * The 409 is the documented "no account connected" answer. Reading it as
     * `false` would delete the Create PR button for every public-repository
     * user, who can still open the dialog and copy the `gh` command.
     */
    fetchMock.mockResolvedValue({ ok: false, status: 409, json: async () => ({}) });
    const { result } = renderHook(() => useRepoWriteAccess("pallets/click"));
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.canWrite).toBeNull();
  });

  it("stays unknown when the request fails outright", async () => {
    fetchMock.mockRejectedValue(new Error("offline"));
    const { result } = renderHook(() => useRepoWriteAccess("o/r"));
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.canWrite).toBeNull();
  });

  it("treats a response with no permissions object as unknown, not writable", async () => {
    // An unauthenticated repository read has no `permissions` key. Absence is
    // not evidence of push, and it is not evidence against it either — the
    // product does not know, so it must not remove the control.
    fetchMock.mockResolvedValue({ ok: true, json: async () => ({ full_name: "o/r" }) });
    const { result } = renderHook(() => useRepoWriteAccess("o/r"));
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.canWrite).toBeNull();
  });

  it("does not ask anything when there is no repository", async () => {
    const { result } = renderHook(() => useRepoWriteAccess(null));
    await waitFor(() => expect(result.current.canWrite).toBeNull());
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("does not keep the previous repository's answer after switching", async () => {
    // Switching repositories mid-flight must not leave repo A's `false` on
    // screen for repo B, which would hide a push that would have worked.
    fetchMock.mockResolvedValue({ ok: true, json: async () => repo(false) });
    const { result, rerender } = renderHook(({ slug }) => useRepoWriteAccess(slug), {
      initialProps: { slug: "o/a" as string | null },
    });
    await waitFor(() => expect(result.current.canWrite).toBe(false));

    fetchMock.mockResolvedValue({ ok: true, json: async () => repo(true) });
    rerender({ slug: "o/b" });
    // Reset to unknown immediately, so the stale answer is never rendered
    // against the new repository even for one frame.
    expect(result.current.canWrite).toBeNull();
    await waitFor(() => expect(result.current.canWrite).toBe(true));
  });
});
