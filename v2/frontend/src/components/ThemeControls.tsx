import { type ReactNode, useEffect, useRef, useState } from "react";
import { Check, Monitor, Moon, Sun } from "lucide-react";
import { ACCENT_PRESETS, type ThemeMode, useTheme } from "../theme";

const MODES: { id: ThemeMode; label: string; icon: ReactNode }[] = [
  { id: "light", label: "Light", icon: <Sun size={14} /> },
  { id: "dark", label: "Dark", icon: <Moon size={14} /> },
  { id: "system", label: "System", icon: <Monitor size={14} /> },
];

/** Light / Dark / System. Instant, no animation (it's a mode switch). */
export function ThemeSwitch() {
  const { mode, setMode } = useTheme();
  return (
    <div className="segmented shrink-0" role="group" aria-label="Theme">
      {MODES.map((m) => (
        <button
          key={m.id}
          type="button"
          className="filter-tab px-2"
          aria-pressed={mode === m.id}
          aria-label={`${m.label} theme`}
          title={m.label}
          onClick={() => setMode(m.id)}
        >
          {m.icon}
        </button>
      ))}
    </div>
  );
}

/** The six house accent presets in a small menu off the header. */
export function AccentMenu() {
  const { accent, setAccent } = useTheme();
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const current = ACCENT_PRESETS.find((p) => p.value.toLowerCase() === accent.toLowerCase());

  useEffect(() => {
    if (!open) return;
    const onPointer = (e: PointerEvent) => {
      if (!root.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("pointerdown", onPointer);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onPointer);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <div ref={root} className="relative">
      <button
        type="button"
        className="btn btn-quiet btn-sm gap-2 px-2.5"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={`Accent color: ${current?.name ?? "custom"}`}
        title="Accent color"
        onClick={() => setOpen((v) => !v)}
      >
        <span className="h-3.5 w-3.5 rounded-full bg-accent shadow-[inset_0_0_0_1px_rgb(0_0_0/0.08)]" />
        <span className="hidden sm:inline">{current?.name ?? "Accent"}</span>
      </button>
      {open && (
        <div role="menu" aria-label="Accent color" className="popover anim-pop-origin-top-right absolute right-0 top-full z-50 mt-2 w-44 p-1.5">
          {ACCENT_PRESETS.map((preset) => {
            const selected = preset === current;
            return (
              <button
                key={preset.value}
                type="button"
                role="menuitemradio"
                aria-checked={selected}
                className="flex h-8 w-full items-center gap-2.5 rounded-[7px] px-2 text-left text-[0.8125rem] font-medium hover:bg-fg/[0.06]"
                onClick={() => {
                  setAccent(preset.value);
                  setOpen(false);
                }}
              >
                <span className="h-3.5 w-3.5 rounded-full" style={{ backgroundColor: preset.value }} />
                {preset.name}
                {selected && <Check size={14} className="ml-auto text-muted" />}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
