import { useEffect, useState } from "react";
import AgentsPage, { useAgentWork } from "./pages/AgentsPage";
import ProjectsPage from "./pages/ProjectsPage";
import ProjectPage from "./pages/ProjectPage";
import IdeasPage from "./pages/IdeasPage";
import IdeaPage from "./pages/IdeaPage";
import SearchPage from "./pages/SearchPage";
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
  const [path, setPath] = useState(window.location.pathname + window.location.search);
  const pathname = path.split("?")[0];
  const project = /^\/projects\/(\d+)\/?$/.exec(pathname);
  const idea = /^\/ideas\/(\d+)\/?$/.exec(pathname);
  const task = /^\/tasks\/(\d+)\/?$/.exec(pathname);
  const [query, setQuery] = useState(new URLSearchParams(window.location.search).get("q") ?? "");
  const work = useAgentWork();
  useEffect(() => {
    const onPop = () => setPath(window.location.pathname + window.location.search);
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);
  useEffect(() => {
    if (pathname === "/search") setQuery(new URLSearchParams(path.split("?")[1]).get("q") ?? "");
  }, [path, pathname]);
  function navigate(event: React.MouseEvent<HTMLElement>, to: string) {
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
            <a href="/" onClick={(event) => navigate(event, "/")} aria-current={pathname === "/" ? "page" : undefined}
              className={`btn btn-sm ${pathname === "/" ? "btn-ghost" : "btn-quiet"}`}>Home</a>
            <a href="/today" onClick={(event) => navigate(event, "/today")} aria-current={pathname === "/today" ? "page" : undefined}
              className={`btn btn-sm ${pathname === "/today" ? "btn-ghost" : "btn-quiet"}`}>Today</a>
            <a href="/projects" onClick={(event) => navigate(event, "/projects")} aria-current={pathname.startsWith("/projects") ? "page" : undefined}
              className={`btn btn-sm ${pathname.startsWith("/projects") ? "btn-ghost" : "btn-quiet"}`}>Projects</a>
            <a href="/ideas" onClick={(event) => navigate(event, "/ideas")} aria-current={pathname.startsWith("/ideas") ? "page" : undefined}
              className={`btn btn-sm ${pathname.startsWith("/ideas") ? "btn-ghost" : "btn-quiet"}`}>Ideas</a>
            <a href="/agents" onClick={(event) => navigate(event, "/agents")} aria-current={pathname === "/agents" ? "page" : undefined}
              className={`btn btn-sm ${pathname === "/agents" ? "btn-ghost" : "btn-quiet"}`}>Agents
              {!!work.data?.waiting_on_dev.length && <span className="tag tabular" aria-label={`${work.data.waiting_on_dev.length} waiting on you`}>{work.data.waiting_on_dev.length}</span>}
            </a>
          </nav>
          <form role="search" className="order-last flex w-full gap-2 sm:order-none sm:w-auto sm:flex-1" onSubmit={(event) => {
            event.preventDefault(); navigateTo(`/search?q=${encodeURIComponent(query.trim())}`);
          }}>
            <input type="search" className="field field-sm min-w-0 flex-1" aria-label="Search tasks, ideas, and notes"
              placeholder="Search tasks, ideas, notes…" maxLength={500} value={query} onChange={(event) => setQuery(event.target.value)} />
            <button className="btn btn-ghost btn-sm">Search</button>
          </form>
          <div className="ml-auto flex items-center gap-1">
            <AccentMenu />
            <ThemeSwitch />
          </div>
        </div>
      </header>
      <main className="mx-auto w-full max-w-[1280px] px-4 pb-24 pt-8 sm:px-8 sm:pt-10" onClick={(event) => {
        const link = (event.target as HTMLElement).closest<HTMLAnchorElement>("a[data-app-link]");
        if (link) navigate(event, link.getAttribute("href")!);
      }}>
        {pathname === "/search" ? <SearchPage key={path} query={new URLSearchParams(path.split("?")[1]).get("q") ?? ""} />
          : pathname === "/today" ? <HomePage key="today" today />
          : task ? <HomePage key={pathname} taskId={Number(task[1])} />
          : pathname === "/agents" ? <AgentsPage work={work} />
          : idea ? <IdeaPage key={idea[1]} id={Number(idea[1])} />
          : /^\/ideas\/?$/.test(pathname) ? <IdeasPage onNavigate={navigateTo} />
          : project ? <ProjectPage key={project[1]} id={Number(project[1])} />
          : /^\/projects\/?$/.test(pathname) ? <ProjectsPage onNavigate={navigateTo} />
          : <HomePage key="home" />}
        <BuildInfo />
      </main>
    </div>
  );
}
