import { type FormEvent, useState } from "react";
import { type Idea, type IdeaStatus, ideaStatuses } from "../lib/ideas";
import type { Project } from "../lib/projects";

export default function IdeaForm({ idea, projects, busy, onSave, onCancel }: {
  idea?: Idea; projects: Project[]; busy: boolean;
  onSave: (body: object) => void; onCancel: () => void;
}) {
  const [title, setTitle] = useState(idea?.title ?? "");
  const [summary, setSummary] = useState(idea?.summary ?? "");
  const [status, setStatus] = useState<IdeaStatus>(idea?.status ?? "seed");
  const [project, setProject] = useState(idea?.project_id?.toString() ?? "");
  function save(event: FormEvent) {
    event.preventDefault();
    if (!busy && title.trim()) onSave({ title, summary, status, project_id: project ? Number(project) : null });
  }
  return <form className="well mb-6 space-y-3 p-4" onSubmit={save}>
    <label className="block"><span className="eyebrow mb-1 block">Idea title</span>
      <input autoFocus required className="field w-full" value={title} disabled={busy} onChange={(e) => setTitle(e.target.value)} /></label>
    <label className="block"><span className="eyebrow mb-1 block">Summary</span>
      <textarea className="field w-full" rows={3} value={summary} disabled={busy} onChange={(e) => setSummary(e.target.value)} /></label>
    <div className="grid gap-3 sm:grid-cols-2">
      <label className="block min-w-0"><span className="eyebrow mb-1 block">Status</span>
        <select className="field w-full" value={status} disabled={busy} onChange={(e) => setStatus(e.target.value as IdeaStatus)}>
          {Object.entries(ideaStatuses).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select></label>
      <label className="block min-w-0"><span className="eyebrow mb-1 block">Project</span>
        <select className="field w-full" value={project} disabled={busy} onChange={(e) => setProject(e.target.value)}>
          <option value="">No project</option>
          {projects.map((project) => <option key={project.id} value={project.id}>{project.title}</option>)}
        </select></label>
    </div>
    <div className="flex gap-2">
      <button className="btn btn-primary btn-sm" disabled={busy || !title.trim()}>{idea ? "Save idea" : "Create idea"}</button>
      <button type="button" className="btn btn-quiet btn-sm" disabled={busy} onClick={onCancel}>Cancel</button>
    </div>
  </form>;
}
