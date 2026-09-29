import { repoSlugFromUrl } from "./github";

export interface IndexedSelection {
  repoUrl: string;
  branch: string;
}
export type OnIndexed = (selection?: IndexedSelection) => void;

// Canonical identity prevents HTTPS/SSH/.git variants creating separate choices.
export function repositoryKey(url: string): string {
  return repoSlugFromUrl(url)?.toLowerCase() ?? url.trim().replace(/\/$/, "");
}
export function branchStorageKey(userId: string, url: string): string {
  return `savflux:${userId}:branch:${repositoryKey(url)}`;
}
export function loadRepoBranch(userId: string, url: string): string {
  try { return localStorage.getItem(branchStorageKey(userId, url)) ?? ""; }
  catch { return ""; }
}
