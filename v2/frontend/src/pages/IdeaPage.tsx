import { useEffect, useState } from "react";
import BlocksEditor from "../components/BlocksEditor";
import IdeaForm from "../components/IdeaForm";
import { api, type Idea, type IdeaDetail, ideaStatuses } from "../lib/ideas";
import { type Project, projectRequest } from "../lib/projects";

export default function IdeaPage({ id }: { id: number }) {
  const [idea, setIdea] = useState<Idea | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function load() {
    setError("");
    try {
      const [idea, projects] = await Promise.all([api<IdeaDetail>(`/ideas/${id}`), projectRequest<Project[]>()]);
      setIdea(idea); setProjects(projects);
    } catch (error) { setError((error as Error).message); }
  }
  useEffect(() => { void load(); }, [id]);
  async function save(body: object) {
    setBusy(true); setError("");
    try { setIdea(await api<Idea>(`/ideas/${id}`, "PATCH", body)); setEditing(false); }
    catch (error) { setError((error as Error).message); }
    finally { setBusy(false); }
  }
  return <div className="anim-fade">
    <a data-app-link href="/ideas" className="mb-5 inline-block text-sm text-muted">← Ideas</a>
    {error && <p role="alert" className="mb-4 text-danger">{error} <button className="btn btn-quiet btn-sm" onClick={load}>Retry</button></p>}
    {!idea ? !error && <p role="status" className="text-muted">Loading idea…</p> : <>
      <header className="mb-5 flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0"><h1 className="page-title [overflow-wrap:anywhere]">{idea.title}</h1>
          <div className="mt-3 flex flex-wrap items-center gap-3">
            <span className="tag tag-accent">{ideaStatuses[idea.status]}</span>
            {idea.project_id && <a data-app-link className="text-sm text-accent underline [overflow-wrap:anywhere]" href={`/projects/${idea.project_id}`}>{projects.find((p) => p.id === idea.project_id)?.title ?? "View project"}</a>}
          </div>
        </div>
        {!editing && <button className="btn btn-ghost btn-sm" onClick={() => setEditing(true)}>Edit idea</button>}
      </header>
      {editing ? <IdeaForm idea={idea} projects={projects} busy={busy} onSave={save} onCancel={() => setEditing(false)} /> :
        idea.summary && <p className="mb-6 whitespace-pre-wrap text-muted [overflow-wrap:anywhere]">{idea.summary}</p>}
      <BlocksEditor owner="ideas" id={id} />
    </>}
  </div>;
}
