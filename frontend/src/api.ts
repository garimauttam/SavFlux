const API_BASE = import.meta.env.VITE_API_URL ?? "";
const OWNER_KEY_SESSION = "savflux:ownerKey";

export function getOwnerKey(): string {
  try {
    return window.sessionStorage.getItem(OWNER_KEY_SESSION) ?? "";
  } catch {
    return "";
  }
}

export function setOwnerKey(key: string): void {
  try {
    window.sessionStorage.setItem(OWNER_KEY_SESSION, key);
  } catch {
    // A disabled session store must not make authentication appear to succeed;
    // the next protected API call will fail and explain the problem.
  }
}

export function clearOwnerKey(): void {
  try {
    window.sessionStorage.removeItem(OWNER_KEY_SESSION);
  } catch {
    // Private browsing can disable storage APIs.
  }
}

export function apiUrl(path: string): string {
  return `${API_BASE}${path}`;
}

export function apiFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers);
  const ownerKey = getOwnerKey();
  if (ownerKey) headers.set("X-API-Key", ownerKey);
  return fetch(apiUrl(path), { ...init, headers });
}
