import React, { createContext, useCallback, useContext, useEffect, useState } from "react";

export type ThemeMode = "light" | "dark" | "system";

export const DEFAULT_ACCENT = "#4f6ef7";

export const ACCENT_PRESETS = [
  { name: "Blue", value: "#4f6ef7" },
  { name: "Violet", value: "#7c5cf5" },
  { name: "Green", value: "#16a36a" },
  { name: "Orange", value: "#ea6a2e" },
  { name: "Pink", value: "#e0457b" },
  { name: "Clay", value: "#be603c" },
];

const MODE_KEY = "todo-theme-mode";
const ACCENT_KEY = "todo-accent";

interface ThemeContextValue {
  mode: ThemeMode;
  accent: string;
  resolvedDark: boolean;
  setMode: (mode: ThemeMode) => void;
  setAccent: (accent: string) => void;
}

const ThemeContext = createContext<ThemeContextValue>({
  mode: "system",
  accent: DEFAULT_ACCENT,
  resolvedDark: false,
  setMode: () => {},
  setAccent: () => {},
});

export function useTheme() {
  return useContext(ThemeContext);
}

function hexToRgbTriplet(hex: string): [number, number, number] | null {
  const match = hex.trim().match(/^#?([0-9a-f]{6})$/i);
  if (!match) return null;
  const value = parseInt(match[1], 16);
  return [(value >> 16) & 255, (value >> 8) & 255, value & 255];
}

/* Pick white or ink text for legibility on the accent. The pre-paint script in index.html
   repeats this (it runs before the bundle), so change both together. */
function accentInkFor([r, g, b]: [number, number, number]): string {
  const luminance = 0.299 * r + 0.587 * g + 0.114 * b;
  return luminance > 160 ? "23 23 23" : "255 255 255";
}

export function applyAccent(accent: string) {
  const rgb = hexToRgbTriplet(accent) ?? hexToRgbTriplet(DEFAULT_ACCENT)!;
  const root = document.documentElement;
  root.style.setProperty("--c-accent", rgb.join(" "));
  root.style.setProperty("--c-accent-ink", accentInkFor(rgb));
}

function systemPrefersDark() {
  return window.matchMedia("(prefers-color-scheme: dark)").matches;
}

function resolveDark(mode: ThemeMode) {
  return mode === "dark" || (mode === "system" && systemPrefersDark());
}

export function ThemeProvider({ children }: { children: React.ReactNode }) {
  const [mode, setModeState] = useState<ThemeMode>(() => {
    const stored = localStorage.getItem(MODE_KEY);
    return stored === "light" || stored === "dark" || stored === "system" ? stored : "system";
  });
  const [accent, setAccentState] = useState<string>(
    () => localStorage.getItem(ACCENT_KEY) ?? DEFAULT_ACCENT
  );
  const [resolvedDark, setResolvedDark] = useState(() => resolveDark(mode));

  useEffect(() => {
    const apply = () => {
      const dark = resolveDark(mode);
      document.documentElement.classList.toggle("dark", dark);
      setResolvedDark(dark);
    };
    apply();
    if (mode !== "system") return;
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    media.addEventListener("change", apply);
    return () => media.removeEventListener("change", apply);
  }, [mode]);

  useEffect(() => {
    applyAccent(accent);
  }, [accent]);

  const setMode = useCallback((next: ThemeMode) => {
    localStorage.setItem(MODE_KEY, next);
    setModeState(next);
  }, []);

  const setAccent = useCallback((next: string) => {
    localStorage.setItem(ACCENT_KEY, next);
    setAccentState(next);
  }, []);

  return (
    <ThemeContext.Provider value={{ mode, accent, resolvedDark, setMode, setAccent }}>
      {children}
    </ThemeContext.Provider>
  );
}
