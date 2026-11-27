import { useEffect, useState } from "react";
import HomePage from "./pages/HomePage";
import { AccentMenu, ThemeSwitch } from "./components/ThemeControls";

/* The mark: a check on a 28px accent square, radius 8. */
function Mark() {
  return (
    <span className="grid h-7 w-7 place-items-center rounded-[8px] bg-accent text-accent-ink shadow-[inset_0_1px_0_rgb(255_255_255/0.18)]">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" className="h-[15px] w-[15px]">
        <path d="M5 12.5l4.5 4.5L19 7.5" />
      </svg>
    </span>
  );
}

interface Health {
  ok: boolean;
  version: string;
  schema_version: number;
}

/** App and schema version from /health, quietly, at the foot of the page. */
function BuildInfo() {
  const [health, setHealth] = useState<Health | null>(null);
  useEffect(() => {
    let live = true;
    fetch("/health")
      .then((r) => (r.ok ? r.json() : null))
      .then((body: Health | null) => live && setHealth(body))
      .catch(() => {});
    return () => {
      live = false;
    };
  }, []);
  if (!health) return null;
  return (
    <p className="tabular mt-16 text-center text-xs text-faint">
      Todo v2 {health.version} · schema {health.schema_version}
    </p>
  );
}

export default function App() {
  return (
    <div className="frame">
      <header className="sticky top-0 z-40 border-b border-edge bg-panel/80 backdrop-blur-xl backdrop-saturate-150">
        <div className="mx-auto flex h-14 max-w-[1280px] items-center gap-6 px-4 sm:px-8">
          <a href="/" className="inline-flex shrink-0 items-center gap-2.5 text-[0.9375rem] font-semibold tracking-[-0.015em]">
            <Mark />
            <span>Todo</span>
            <span className="tag">v2</span>
          </a>
          <div className="ml-auto flex items-center gap-1">
            <AccentMenu />
            <ThemeSwitch />
          </div>
        </div>
      </header>
      <main className="mx-auto w-full max-w-[1280px] px-4 pb-24 pt-8 sm:px-8 sm:pt-10">
        <HomePage />
        <BuildInfo />
      </main>
    </div>
  );
}
