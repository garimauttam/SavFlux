/**
 * github.ts — the small amount of GitHub knowledge the UI needs.
 *
 * `owner/name` is the slug every GitHub REST path takes, and a repository URL
 * is what SavFlux stores. Turning one into the other is a line of regex, but it
 * is needed in the top bar (branch list), the repository browser and the
 * palette, and each of those used to grow its own slightly different version —
 * one of which stripped `.git` and two of which did not, so a repo indexed from
 * an SSH URL and the same repo indexed from HTTPS produced two different
 * entries in the same list.
 */

const HTTP_RE = /^https?:\/\/(?:[^@/]+@)?github\.com\/([^/\s]+)\/([^/\s#?]+)/i;
const SSH_RE = /^git@github\.com:([^/\s]+)\/([^\s]+?)(?:\.git)?$/i;

/** `https://github.com/o/r`, `git@github.com:o/r.git` → `o/r`. Null if neither. */
export function repoSlugFromUrl(url: string | null | undefined): string | null {
  if (!url) return null;
  const value = url.trim();
  const http = HTTP_RE.exec(value);
  if (http) return `${http[1]}/${http[2].replace(/\.git$/, "")}`;
  const ssh = SSH_RE.exec(value);
  if (ssh) return `${ssh[1]}/${ssh[2]}`;
  const bare = /^([A-Za-z0-9_.-]+)\/([A-Za-z0-9_.-]+)$/.exec(value);
  if (bare) return `${bare[1]}/${bare[2]}`;
  return null;
}

/** The last path segment — what a person recognises a repository by. */
export function repoDisplayName(url: string | null | undefined): string {
  if (!url) return "No repository";
  return url
    .replace(/^https?:\/\/(?:[^@/]+@)?github\.com\//i, "")
    .replace(/^git@github\.com:/i, "")
    .replace(/\.git$/, "")
    .replace(/\/$/, "");
}

/** Do two stored URLs name the same repository? Case-insensitive, .git-agnostic. */
export function sameRepo(a: string | null, b: string | null): boolean {
  if (!a || !b) return a === b;
  const norm = (u: string) => u.trim().toLowerCase().replace(/\.git$/, "").replace(/\/$/, "");
  return norm(a) === norm(b);
}
