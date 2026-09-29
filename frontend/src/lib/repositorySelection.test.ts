import { beforeEach, expect, it, vi } from "vitest";
import { branchStorageKey, loadRepoBranch, repositoryKey } from "./repositorySelection";

beforeEach(() => localStorage.clear());
it("restores the same repository across URL formats but isolates accounts and repos", () => {
  localStorage.setItem(branchStorageKey("user-1", "https://github.com/Owner/Repo.git"), "feature/auth");
  expect(repositoryKey("git@github.com:owner/repo.git")).toBe("owner/repo");
  expect(loadRepoBranch("user-1", "git@github.com:owner/repo.git")).toBe("feature/auth");
  expect(loadRepoBranch("user-2", "https://github.com/owner/repo")).toBe("");
  expect(loadRepoBranch("user-1", "https://github.com/owner/another")).toBe("");
});
it("handles unavailable local storage", () => {
  vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("blocked"); });
  expect(loadRepoBranch("user-1", "https://github.com/owner/repo")).toBe("");
});
