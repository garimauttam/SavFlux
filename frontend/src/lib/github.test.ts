/**
 * github.test.ts — the slug rules, pinned.
 *
 * These three functions exist because four components had four versions of the
 * same line. Two of them only understood `https://`, so a repository indexed
 * from an SSH URL rendered as `git@github.com:owner/name` in the top bar and
 * `garimauttam/SavFlux` in the browser — the same repo, two different names, on
 * one screen. The tests below are mostly about that: the awkward inputs, not the
 * happy path.
 */

import { describe, expect, it } from "vitest";
import { repoDisplayName, repoSlugFromUrl, sameRepo } from "./github";
import { sourceFileName } from "./openFile";

describe("repoSlugFromUrl", () => {
  it("reads the slug out of every URL shape a repository is stored as", () => {
    for (const url of [
      "https://github.com/pallets/click",
      "https://github.com/pallets/click.git",
      "https://github.com/pallets/click/",
      "http://github.com/pallets/click",
      "https://token@github.com/pallets/click",
      "git@github.com:pallets/click.git",
      "git@github.com:pallets/click",
      "pallets/click",
    ]) {
      expect(repoSlugFromUrl(url), url).toBe("pallets/click");
    }
  });

  it("returns null for something that is not a repository", () => {
    expect(repoSlugFromUrl("")).toBeNull();
    expect(repoSlugFromUrl(null)).toBeNull();
    expect(repoSlugFromUrl("not a url at all")).toBeNull();
  });

  it("stops at a query string or fragment", () => {
    expect(repoSlugFromUrl("https://github.com/o/r/tree/main?tab=readme")).toBe("o/r");
    expect(repoSlugFromUrl("https://github.com/o/r#readme")).toBe("o/r");
  });
});

describe("repoDisplayName", () => {
  it("shows the slug for both URL schemes, not the SSH transport", () => {
    // The bug this function was written for: the top bar used to fall back to
    // the raw URL, so an SSH-indexed repo read `git@github.com:o/r`.
    expect(repoDisplayName("https://github.com/pallets/click")).toBe("pallets/click");
    expect(repoDisplayName("git@github.com:pallets/click.git")).toBe("pallets/click");
  });

  it("strips a trailing slash and .git", () => {
    expect(repoDisplayName("https://github.com/o/r.git")).toBe("o/r");
    expect(repoDisplayName("https://github.com/o/r/")).toBe("o/r");
  });

  it("says so when there is no repository", () => {
    expect(repoDisplayName(null)).toBe("No repository");
    expect(repoDisplayName("")).toBe("No repository");
  });
});

describe("sameRepo", () => {
  it("treats the same repo written three ways as one repo", () => {
    expect(sameRepo("https://github.com/o/r", "https://github.com/o/r.git")).toBe(true);
    expect(sameRepo("https://github.com/o/r", "HTTPS://GITHUB.COM/O/R")).toBe(true);
    expect(sameRepo("https://github.com/o/r/", "https://github.com/o/r")).toBe(true);
  });

  it("still separates two different repos, and two empty ones", () => {
    expect(sameRepo("https://github.com/o/r", "https://github.com/o/other")).toBe(false);
    expect(sameRepo(null, null)).toBe(true);
    expect(sameRepo(null, "https://github.com/o/r")).toBe(false);
  });
});

describe("sourceFileName", () => {
  it("strips the repository prefix a source id carries", () => {
    expect(sourceFileName("https://github.com/o/r::src/auth/tokens.py")).toBe("tokens.py");
    expect(sourceFileName("src/auth/tokens.py")).toBe("tokens.py");
    expect(sourceFileName("tokens.py")).toBe("tokens.py");
  });

  it("splits on the last separator, not the first", () => {
    // `split("::").pop()` and `lastIndexOf` agree; a naive `indexOf` would cut
    // the URL itself in half.
    expect(sourceFileName("https://github.com/o/r::a::b/c.py")).toBe("c.py");
  });
});
