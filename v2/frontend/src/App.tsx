import { useEffect, useState } from "react";
import AgentsPage, { useAgentWork } from "./pages/AgentsPage";
import ProjectsPage from "./pages/ProjectsPage";
import ProjectPage from "./pages/ProjectPage";
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
  const [path, setPath] = useState(window.location.pathname);
  const project = /^\/projects\/(\d+)\/?$/.exec(path);
  const work = useAgentWork();
  useEffect(() => {
    const onPop = () => setPath(window.location.pathname);
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);
  function navigate(event: React.MouseEvent<HTMLAnchorElement>, to: string) {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    navigateTo(to);
  }
  function navigateTo(to: string) {
    window.history.pushState(null, "", to);
    setPath(to);
    window.scrollTo(0, 0);
  }
  return (
    <div className="frame">
      <header className="sticky top-0 z-40 border-b border-edge bg-panel/80 backdrop-blur-xl backdrop-saturate-150">
        <div className="mx-auto flex min-h-14 flex-wrap py-2 max-w-[1280px] items-center gap-2 px-4 sm:gap-6 sm:px-8">
          <a href="/" onClick={(event) => navigate(event, "/")} className="inline-flex shrink-0 items-center gap-2.5 text-[0.9375rem] font-semibold tracking-[-0.015em]">
            <Mark />
            <span>Todo</span>
            <span className="tag">v2</span>
          </a>
          <nav aria-label="Main navigation" className="order-last flex w-full items-center gap-1 sm:order-none sm:w-auto">
            <a href="/" onClick={(event) => navigate(event, "/")} aria-current={path === "/" ? "page" : undefined}
              className={`btn btn-sm ${path === "/" ? "btn-ghost" : "btn-quiet"}`}>Home</a>
            <a href="/projects" onClick={(event) => navigate(event, "/projects")} aria-current={path.startsWith("/projects") ? "page" : undefined}
              className={`btn btn-sm ${path.startsWith("/projects") ? "btn-ghost" : "btn-quiet"}`}>Projects</a>
            <a href="/agents" onClick={(event) => navigate(event, "/agents")} aria-current={path === "/agents" ? "page" : undefined}
              className={`btn btn-sm ${path === "/agents" ? "btn-ghost" : "btn-quiet"}`}>Agents
              {!!work.data?.waiting_on_dev.length && <span className="tag tabular" aria-label={`${work.data.waiting_on_dev.length} waiting on you`}>{work.data.waiting_on_dev.length}</span>}
            </a>
          </nav>
          <div className="ml-auto flex items-center gap-1">
            <AccentMenu />
            <ThemeSwitch />
          </div>
        </div>
      </header>
      <main className="mx-auto w-full max-w-[1280px] px-4 pb-24 pt-8 sm:px-8 sm:pt-10">
        {path === "/agents" ? <AgentsPage work={work} /> : project ? <ProjectPage key={project[1]} id={Number(project[1])} /> : /^\/projects\/?$/.test(path) ? <ProjectsPage onNavigate={navigateTo} /> : <HomePage />}
        <BuildInfo />
      </main>
    </div>
  );
}
