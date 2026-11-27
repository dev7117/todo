import { useEffect, useState } from "react";
import IdeaForm from "../components/IdeaForm";
import { api, type Idea, ideaStatuses } from "../lib/ideas";
import { type Project, projectRequest } from "../lib/projects";

export default function IdeasPage({ onNavigate }: { onNavigate: (to: string) => void }) {
  const [ideas, setIdeas] = useState<Idea[] | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [status, setStatus] = useState("");
  const [project, setProject] = useState("");
  const [adding, setAdding] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function load() {
    setError("");
    try {
      const [rows, projects] = await Promise.all([api<Idea[]>("/ideas"), projectRequest<Project[]>()]);
      setIdeas(rows); setProjects(projects);
    } catch (error) { setError((error as Error).message); }
  }
  useEffect(() => { void load(); }, []);
  async function create(body: object) {
    setBusy(true); setError("");
    try { const row = await api<Idea>("/ideas", "POST", body); onNavigate(`/ideas/${row.id}`); }
    catch (error) { setError((error as Error).message); }
    finally { setBusy(false); }
  }
  return <div className="anim-fade">
    <header className="mb-6 flex items-start justify-between gap-3">
      <div><h1 className="page-title">Ideas</h1><p className="mt-1 text-muted">Room for possibilities.</p></div>
      <button className="btn btn-primary btn-sm" disabled={ideas === null || adding} onClick={() => setAdding(true)}>New idea</button>
    </header>
    {error && <p role="alert" className="mb-4 text-danger">{error} <button className="btn btn-quiet btn-sm" onClick={load}>Retry</button></p>}
    {adding && <IdeaForm projects={projects} busy={busy} onSave={create} onCancel={() => setAdding(false)} />}
    <div className="mb-6 grid gap-3 sm:grid-cols-2">
      <label className="min-w-0"><span className="eyebrow mb-1 block">Status</span>
        <select className="field w-full" value={status} onChange={(e) => setStatus(e.target.value)}>
          <option value="">All statuses</option>{Object.entries(ideaStatuses).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select></label>
      <label className="min-w-0"><span className="eyebrow mb-1 block">Project</span>
        <select className="field w-full" value={project} onChange={(e) => setProject(e.target.value)}>
          <option value="">All projects</option>{projects.map((p) => <option key={p.id} value={p.id}>{p.title}</option>)}
        </select></label>
    </div>
    {ideas === null ? !error && <p role="status" className="text-muted">Loading ideas…</p> : <div className="space-y-6">
      {Object.entries(ideaStatuses).filter(([value]) => !status || status === value).map(([value, label]) => {
        const group = ideas.filter((idea) => idea.status === value && (!project || idea.project_id === Number(project)));
        return <section key={value} aria-label={`${label} ideas`}>
          <h2 className="section-title mb-2">{label} <span className="text-muted">({group.length})</span></h2>
          <div className="well">{group.length ? <ul className="divide-y divide-edge">{group.map((idea) => <li key={idea.id}>
            <a href={`/ideas/${idea.id}`} className="block p-4 hover:bg-panel [overflow-wrap:anywhere]" onClick={(e) => {
              if (e.button || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
              e.preventDefault(); onNavigate(`/ideas/${idea.id}`);
            }}><span className="font-medium">{idea.title}</span>
              {idea.summary && <p className="mt-1 line-clamp-2 text-sm text-muted">{idea.summary}</p>}
              {idea.project_id && <span className="tag mt-2">{projects.find((p) => p.id === idea.project_id)?.title ?? "Project"}</span>}
            </a>
          </li>)}</ul> : <p className="p-4 text-sm text-muted">No {label.toLowerCase()} ideas here yet.</p>}</div>
        </section>;
      })}
    </div>}
  </div>;
}
