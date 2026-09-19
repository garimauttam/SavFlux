/**
 * ThemeToggle.tsx — Light / dark / system theme switch (P2 Theme, $0).
 *
 * Mechanism: toggles the `dark` class on <html> (tailwind darkMode: "class")
 * and persists the choice in localStorage. "system" follows
 * prefers-color-scheme and live-updates when the OS theme changes.
 *
 * Components use fixed dark utilities, so index.css also ships a light-mode
 * override layer (html:not(.dark) …) that remaps the common surfaces.
 */

import { useCallback, useEffect, useState } from "react";
import { Sun, Moon, Monitor } from "lucide-react";

type Theme = "light" | "dark" | "system";

const STORAGE_KEY = "savflux:theme";

function loadTheme(): Theme {
  try {
    const v = localStorage.getItem(STORAGE_KEY);
    if (v === "light" || v === "dark" || v === "system") return v;
  } catch {}
  return "dark"; // default preserves the classic SavFlux look
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

function applyTheme(theme: Theme): void {
  const dark = resolveDark(theme);
  document.documentElement.classList.toggle("dark", dark);
  document.documentElement.style.colorScheme = dark ? "dark" : "light";
  try {
    localStorage.setItem(STORAGE_KEY, theme);
  } catch {}
}

export function ThemeToggle({ compact = false }: { compact?: boolean }) {
  const [theme, setTheme] = useState<Theme>(loadTheme);

  useEffect(() => {
    applyTheme(theme);
  }, [theme]);

  // Live-follow the OS theme while in "system" mode
  useEffect(() => {
    if (theme !== "system") return;
    let mq: MediaQueryList | null = null;
    try {
      mq = window.matchMedia("(prefers-color-scheme: dark)");
    } catch {
      return;
    }
    const handler = () => applyTheme("system");
    mq.addEventListener("change", handler);
    return () => mq?.removeEventListener("change", handler);
  }, [theme]);

  const cycle = useCallback(() => {
    setTheme((t) => (t === "dark" ? "light" : t === "light" ? "system" : "dark"));
  }, []);

  const Icon = theme === "dark" ? Moon : theme === "light" ? Sun : Monitor;
  const label = `Theme: ${theme} (click to change)`;

  if (compact) {
    return (
      <button
        onClick={cycle}
        title={label}
        aria-label={label}
        className="p-1.5 rounded-full text-gray-400 hover:text-white hover:bg-gray-800 transition-colors"
      >
        <Icon className="w-4 h-4" />
      </button>
    );
  }

  return (
    <div className="flex items-center gap-1 rounded-lg border border-gray-700 bg-gray-800 p-1" title={label}>
      {(["light", "dark", "system"] as Theme[]).map((t) => {
        const TIcon = t === "dark" ? Moon : t === "light" ? Sun : Monitor;
        const active = theme === t;
        return (
          <button
            key={t}
            onClick={() => setTheme(t)}
            title={`Use ${t} theme`}
            aria-pressed={active}
            className={`p-1.5 rounded-md transition-colors ${
              active ? "bg-gray-700 text-white" : "text-gray-500 hover:text-gray-300"
            }`}
          >
            <TIcon className="w-4 h-4" />
          </button>
        );
      })}
    </div>
  );
}

export default ThemeToggle;
