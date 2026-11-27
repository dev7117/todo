import { type FormEvent, useEffect, useState } from "react";
import ProjectProgress from "../components/ProjectProgress";
import { type Project, projectRequest, projectStatuses } from "../lib/projects";

export default function ProjectsPage() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [title, setTitle] = useState("");
  const [busy, setBusy] = useState(false);
  async function load() {
    setLoading(true); setError("");
    try { setProjects(await projectRequest<Project[]>()); }
    catch (error) { setError((error as Error).message); }
    finally { setLoading(false); }
  }
  useEffect(() => { void load(); }, []);
  async function add(event: FormEvent) {
    event.preventDefault();
    if (busy || !title.trim()) return;
    setBusy(true); setError("");
    try {
      const project = await projectRequest<Project>("", "POST", { title });
      window.location.assign(`/projects/${project.id}`);
    } catch (error) { setError((error as Error).message); setBusy(false); }
  }
  return <div className="anim-fade">
    <header className="mb-8">
      <h1 className="page-title">Projects</h1>
      <p className="mt-1 text-muted">A home for the things you’re moving forward.</p>
    </header>
    <form className="mb-6 flex gap-2" onSubmit={add}>
      <input className="field min-w-0 flex-1" aria-label="New project" placeholder="Add a project…"
        required value={title} disabled={busy || loading} onChange={(event) => setTitle(event.target.value)} />
      <button className="btn btn-primary" disabled={busy || loading || !title.trim()}>Add</button>
    </form>
    {error && <div role="alert" className="mb-4 text-danger">{error}
      <button className="btn btn-quiet btn-sm" onClick={load}>Retry</button></div>}
    {loading ? <p role="status" className="text-muted">Loading projects…</p> : projects.length ? (
      <ul className="grid gap-4 md:grid-cols-2">
        {projects.map((project) => <li key={project.id} className="well min-w-0">
          <a href={`/projects/${project.id}`} className="block space-y-4 p-5">
            <div className="flex items-start justify-between gap-3">
              <h2 className="font-semibold [overflow-wrap:anywhere]">{project.title}</h2>
              <span className={`tag shrink-0 ${project.status === "active" ? "tag-accent" : ""}`}>{projectStatuses[project.status]}</span>
            </div>
            {project.description && <p className="line-clamp-2 whitespace-pre-wrap text-sm text-muted [overflow-wrap:anywhere]">{project.description}</p>}
            {project.target_on && <p className="text-xs text-muted">Target <time dateTime={project.target_on}>{project.target_on}</time></p>}
            <ProjectProgress progress={project.progress} title={project.title} />
          </a>
        </li>)}
      </ul>
    ) : !error && <div className="well px-6 py-16 text-center text-muted">No projects yet. Add one when you’re ready.</div>}
  </div>;
}
