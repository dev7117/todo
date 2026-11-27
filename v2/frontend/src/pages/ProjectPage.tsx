import { type FormEvent, useEffect, useState } from "react";
import { api, type Idea, ideaStatuses } from "../lib/ideas";
import TaskRow from "../components/TaskRow";
import ProjectProgress from "../components/ProjectProgress";
import { type Project, type ProjectDetail, type ProjectStatus, projectRequest, projectStatuses } from "../lib/projects";
import { isFinished, request, type Task, taskTree } from "../lib/tasks";

export default function ProjectPage({ id }: { id: number }) {
  const [project, setProject] = useState<ProjectDetail | null>(null);
  const [ideas, setIdeas] = useState<Idea[]>([]);
  const [projects, setProjects] = useState<Project[]>([]);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [status, setStatus] = useState<ProjectStatus>("active");
  const [target, setTarget] = useState("");

  async function load() {
    setError("");
    try {
      const [detail, allProjects, allTasks, linkedIdeas] = await Promise.all([
        projectRequest<ProjectDetail>(`/${id}`), projectRequest<Project[]>(), request<Task[]>(), api<Idea[]>(`/ideas?project_id=${id}`),
      ]);
      setProject(detail); setProjects(allProjects); setTasks(allTasks); setIdeas(linkedIdeas);
    } catch (error) { setError((error as Error).message); }
  }
  useEffect(() => { void load(); }, [id]);
  async function save(body: object) {
    setBusy(true); setError("");
    try {
      const updated = await projectRequest<Project>(`/${id}`, "PATCH", body);
      setProject((old) => old ? { ...old, ...updated } : null);
      setEditing(false);
    } catch (error) { setError((error as Error).message); }
    finally { setBusy(false); }
  }
  function edit(event: FormEvent) {
    event.preventDefault();
    if (!busy && title.trim()) void save({ title, description, status, target_on: target || null });
  }
  const rows = (group: Task[]) => taskTree(group).map(({ task, depth }) => <TaskRow
    key={task.id} task={task} tasks={tasks} projects={projects} depth={depth}
    onChange={() => void load()} onAdd={() => void load()} />);

  return <div className="anim-fade">
    <a href="/projects" className="mb-5 inline-block text-sm text-muted">← Projects</a>
    {error && <div role="alert" className="mb-4 text-danger">{error}
      <button className="btn btn-quiet btn-sm" onClick={load}>Retry</button></div>}
    {!project ? !error && <p role="status" className="text-muted">Loading project…</p> : <>
      <header className="mb-6 flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0">
          <h1 className="page-title [overflow-wrap:anywhere]">{project.title}</h1>
          <div className="mt-3 flex flex-wrap items-center gap-3">
            <span className="tag tag-accent">{projectStatuses[project.status]}</span>
            {project.target_on && <span className="text-sm text-muted">Target <time dateTime={project.target_on}>{project.target_on}</time></span>}
          </div>
        </div>
        <div className="flex gap-2">
          <button className="btn btn-ghost btn-sm" disabled={busy} onClick={() => {
            setTitle(project.title); setDescription(project.description); setStatus(project.status);
            setTarget(project.target_on ?? ""); setEditing(!editing);
          }}>{editing ? "Cancel edit" : "Edit project"}</button>
          {project.status !== "done" && <button className="btn btn-primary btn-sm" disabled={busy || editing}
            onClick={() => void save({ status: "done" })}>Mark done</button>}
        </div>
      </header>
      {editing ? <form className="well mb-6 space-y-3 p-4" onSubmit={edit}>
        <label className="block"><span className="eyebrow mb-1 block">Project title</span>
          <input className="field w-full" required value={title} disabled={busy} onChange={(e) => setTitle(e.target.value)} /></label>
        <label className="block"><span className="eyebrow mb-1 block">Description (Markdown)</span>
          <textarea className="field w-full" rows={3} value={description} disabled={busy} onChange={(e) => setDescription(e.target.value)} /></label>
        <div className="grid gap-3 sm:grid-cols-2">
          <label className="block min-w-0"><span className="eyebrow mb-1 block">Status</span>
            <select className="field w-full" value={status} disabled={busy} onChange={(e) => setStatus(e.target.value as ProjectStatus)}>
              {Object.entries(projectStatuses).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
            </select></label>
          <label className="block min-w-0"><span className="eyebrow mb-1 block">Target date</span>
            <input type="date" className="field w-full min-w-0" value={target} disabled={busy} onChange={(e) => setTarget(e.target.value)} /></label>
        </div>
        <button className="btn btn-primary btn-sm" disabled={busy || !title.trim()}>Save project</button>
      </form> : project.description && <p className="mb-6 whitespace-pre-wrap text-muted [overflow-wrap:anywhere]">{project.description}</p>}
      <div className="well mb-8 p-5">
        <ProjectProgress progress={project.progress} title={project.title} />
        <p className="mt-3 text-xs text-muted">Counts tasks without non-cancelled subtasks in this project.</p>
      </div>
      <section className="mb-8" aria-label="Project ideas">
        <h2 className="section-title mb-1">Ideas</h2>
        <p className="mb-3 text-xs text-muted">Context and possibilities. Ideas don’t count toward progress.</p>
        <div className="well">{ideas.length ? <ul className="divide-y divide-edge">{ideas.map((idea) => <li key={idea.id}>
          <a data-app-link href={`/ideas/${idea.id}`} className="flex flex-wrap items-center justify-between gap-2 p-4">
            <span className="min-w-0 [overflow-wrap:anywhere]">{idea.title}</span><span className="tag">{ideaStatuses[idea.status]}</span>
          </a>
        </li>)}</ul> : <p className="p-4 text-sm text-muted">No linked ideas yet. <a data-app-link className="underline" href="/ideas">Browse ideas</a></p>}</div>
      </section>
      <div className="mb-3 flex items-center justify-between gap-3">
        <h2 className="section-title">Tasks</h2><a href="/" className="btn btn-quiet btn-sm">Manage tasks</a>
      </div>
      <section className="well" aria-label="Active project tasks">
        {project.tasks.some((t) => !isFinished(t)) ? <ul>{rows(project.tasks.filter((t) => !isFinished(t)))}</ul> :
          <p className="p-5 text-sm text-muted">No active tasks. Assign tasks from the Tasks page.</p>}
      </section>
      {project.tasks.some(isFinished) && <details className="well mt-6">
        <summary className="cursor-pointer p-4 font-medium">Done and cancelled ({project.tasks.filter(isFinished).length})</summary>
        <ul className="border-t border-edge">{rows(project.tasks.filter(isFinished))}</ul>
      </details>}
    </>}
  </div>;
}
