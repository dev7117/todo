import { type FormEvent, useEffect, useRef, useState } from "react";
import FromV1 from "../components/FromV1";

import TaskRow from "../components/TaskRow";
import { isFinished, request, type Task, taskTree } from "../lib/tasks";

export default function HomePage() {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [title, setTitle] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [error, setError] = useState("");
  const addInput = useRef<HTMLInputElement>(null);

  async function load() {
    setLoading(true);
    setLoadError("");
    try { setTasks(await request<Task[]>()); }
    catch (error) { setLoadError((error as Error).message); }
    finally { setLoading(false); }
  }
  useEffect(() => { void load(); }, []);

  async function add(event: FormEvent) {
    event.preventDefault();
    if (busy || !title.trim()) return;
    setBusy(true);
    setError("");
    try {
      const task = await request<Task>("", "POST", { title });
      setTasks((current) => [...current, task]);
      setTitle("");
    } catch (error) { setError((error as Error).message); }
    finally {
      setBusy(false);
      requestAnimationFrame(() => addInput.current?.focus());
    }
  }

  const update = (task: Task) => setTasks((current) => current.map((old) => old.id === task.id ? task : old));
  const open = tasks.filter((task) => !isFinished(task));
  const done = tasks.filter(isFinished);
  const append = (task: Task) => setTasks((current) => [...current, task]);
  const rows = (group: Task[]) => taskTree(group).map(({ task, depth }) => (
    <TaskRow key={task.id} task={task} tasks={tasks} depth={depth} onChange={update} onAdd={append} />
  ));

  return (
    <div className="anim-fade">
      <header className="mb-8">
        <h1 className="page-title">Tasks</h1>
        <p className="mt-1 text-muted">A little room for what needs doing.</p>
      </header>
      <form className="mb-6 flex gap-2" onSubmit={add}>
        <input ref={addInput} className="field min-w-0 flex-1" aria-label="New task" placeholder="Add a task…"
          required value={title} disabled={busy || loading || !!loadError} onChange={(event) => setTitle(event.target.value)} />
        <button className="btn btn-primary" disabled={busy || loading || !!loadError || !title.trim()}>Add</button>
      </form>
      {error && <p role="alert" className="mb-4 text-sm text-danger">{error}</p>}
      {loading ? <p role="status" className="text-muted">Loading tasks…</p> : loadError ? (
        <div role="alert" className="well p-4">
          <p className="mb-2 text-danger">{loadError}</p>
          <button className="btn btn-ghost btn-sm" onClick={load}>Retry</button>
        </div>
      ) : (
        <>
          <section aria-label="Active tasks" className="well">
            {open.length ? <ul>{rows(open)}</ul> : (
              <div className="px-6 py-16 text-center">
                <p className="font-medium">No active tasks</p>
                <p className="mt-1 text-sm text-muted">Add something above when you’re ready.</p>
              </div>
            )}
          </section>
          {done.length > 0 && (
            <details className="well mt-6">
              <summary className="cursor-pointer p-4 font-medium">Done and cancelled <span className="ml-1 text-muted">({done.length})</span></summary>
              <ul className="border-t border-edge">{rows(done)}</ul>
            </details>
          )}
        </>
      )}
      <FromV1 />
    </div>
  );
}
