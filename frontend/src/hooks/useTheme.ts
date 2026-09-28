/**
 * useTheme.ts — one owner for the light/dark decision.
 *
 * WHY THIS IS A HOOK AND NOT A CLASSLIST TOGGLE
 * ---------------------------------------------
 * The old shell toggled `document.documentElement.classList` from three places
 * — the top bar, the command palette and nothing else that mattered — and the
 * *initial* class was applied by `ThemeToggle`'s mount effect. That is a flash
 * of the wrong theme on every load, and it only worked because one particular
 * component happened to be mounted. Removing that component (which the redesign
 * did, deliberately) silently removed the only code that set the theme at all:
 * the app came up light regardless of the stored preference.
 *
 * So the class is applied by an inline script in `index.html` before first
 * paint, and this hook owns every subsequent change. Two rules it enforces:
 * a stored preference is honoured, and "system" follows the OS live.
 */

import { useCallback, useEffect, useState } from "react";

export type Theme = "light" | "dark" | "system";

const STORAGE_KEY = "savflux:theme";

function read(): Theme {
  try {
    const v = localStorage.getItem(STORAGE_KEY);
    if (v === "light" || v === "dark" || v === "system") return v;
  } catch {
    /* private mode — fall through to the default */
  }
  return "dark";
}

function resolveDark(theme: Theme): boolean {
  if (theme === "dark") return true;
  if (theme === "light") return false;
  try {
    return window.matchMedia("(prefers-color-scheme: dark)").matches;
  } catch {
    return true;
  }
}

function apply(theme: Theme): void {
  const dark = resolveDark(theme);
  document.documentElement.classList.toggle("dark", dark);
  document.documentElement.style.colorScheme = dark ? "dark" : "light";
  try {
    localStorage.setItem(STORAGE_KEY, theme);
  } catch {
    /* private mode — the choice just does not persist */
  }
}

export function useTheme() {
  const [theme, setTheme] = useState<Theme>(read);
  const [isDark, setIsDark] = useState(() => resolveDark(read()));

  useEffect(() => {
    apply(theme);
    setIsDark(resolveDark(theme));
  }, [theme]);

  // "system" is a live subscription, not a one-time read: a user who switches
  // their OS to light should see the product follow without a reload.
  useEffect(() => {
    if (theme !== "system") return;
    let mq: MediaQueryList | null = null;
    try {
      mq = window.matchMedia("(prefers-color-scheme: dark)");
    } catch {
      return;
    }
    const handler = () => {
      apply("system");
      setIsDark(resolveDark("system"));
    };
    mq.addEventListener("change", handler);
    return () => mq?.removeEventListener("change", handler);
  }, [theme]);

  const toggle = useCallback(() => {
    setTheme((t) => (resolveDark(t) ? "light" : "dark"));
  }, []);

  return { theme, setTheme, isDark, toggle };
}
