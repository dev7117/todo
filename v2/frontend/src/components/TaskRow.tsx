import { type FormEvent, useEffect, useRef, useState } from "react";
import { canParent, type HistoryEntry, request, statuses, type Status, type Task } from "../lib/tasks";

function historyValue(entry: HistoryEntry, value: string | null) {
  if (entry.field === "status") return statuses[value as Status] ?? value;
  if (entry.field === "parent_id") return value === null ? "No parent" : `Task #${value}`;
  return value || "Empty";
}

export default function TaskRow({ task, tasks, depth, onChange, onAdd }: {
  task: Task; tasks: Task[]; depth: number;
  onChange: (task: Task) => void; onAdd: (task: Task) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState(task.title);
  const [notes, setNotes] = useState(task.notes);
  const [parentId, setParentId] = useState(task.parent_id?.toString() ?? "");
  const [subtask, setSubtask] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [history, setHistory] = useState<HistoryEntry[] | null>(null);
  const [historyError, setHistoryError] = useState("");
  const [historyRetry, setHistoryRetry] = useState(0);
  const editButton = useRef<HTMLButtonElement>(null);
  const subtaskInput = useRef<HTMLInputElement>(null);
  const parent = tasks.find((candidate) => candidate.id === task.parent_id);

  useEffect(() => {
    if (!editing) return;
    let live = true;
    setHistory(null);
    setHistoryError("");
    request<HistoryEntry[]>(`/${task.id}/history`)
      .then((entries) => { if (live) setHistory(entries); })
      .catch(() => { if (live) setHistoryError("Could not load history."); });
    return () => { live = false; };
  }, [editing, task.id, task.updated_at, historyRetry]);

  function closeEditor() {
    setEditing(false);
    requestAnimationFrame(() => editButton.current?.focus());
  }

  async function change(body: object) {
    setBusy(true);
    setError("");
    try { onChange(await request<Task>(`/${task.id}`, "PATCH", body)); }
    catch (error) { setError((error as Error).message); }
    finally { setBusy(false); }
  }

  async function save(event: FormEvent) {
    event.preventDefault();
    if (busy || !title.trim()) return;
    await change({ title, notes, parent_id: parentId ? Number(parentId) : null });
  }

  async function addSubtask(event: FormEvent) {
    event.preventDefault();
    if (busy || !subtask.trim()) return;
    setBusy(true);
    setError("");
    try {
      onAdd(await request<Task>("", "POST", { title: subtask, parent_id: task.id }));
      setSubtask("");
    } catch (error) { setError((error as Error).message); }
    finally {
      setBusy(false);
      requestAnimationFrame(() => subtaskInput.current?.focus());
    }
  }

  return (
    <li className="border-b border-edge last:border-b-0" style={{ paddingLeft: Math.min(depth, 4) * 16 }}>
      <div className="flex flex-wrap items-start gap-3 p-4">
        <input type="checkbox" className="mt-1 h-4 w-4 shrink-0 cursor-pointer"
          aria-label={`${task.status === "done" ? "Reopen" : "Complete"} ${task.title}`}
          checked={task.status === "done"} disabled={busy || editing}
          onChange={() => void change({ status: task.status === "done" ? "todo" : "done" })} />
        <button ref={editButton} type="button" className="min-w-0 flex-1 text-left [overflow-wrap:anywhere]"
          aria-label={`Edit ${task.title}`} aria-expanded={editing} disabled={busy}
          onClick={() => {
            if (editing) { closeEditor(); return; }
            setTitle(task.title); setNotes(task.notes); setParentId(task.parent_id?.toString() ?? "");
            setError(""); setEditing(true);
          }}>
          <span className={task.status === "done" || task.status === "cancelled" ? "text-muted line-through" : "font-medium"}>
            {task.title}
          </span>
          {parent && <span className="mt-1 block text-xs text-muted">Subtask of {parent.title}</span>}
          {task.notes && !editing && <span className="mt-1 block whitespace-pre-wrap text-sm text-muted">{task.notes}</span>}
        </button>
        <select aria-label={`Status for ${task.title}`} className="field field-sm max-w-full" value={task.status}
          disabled={busy || editing} onChange={(event) => void change({ status: event.target.value })}>
          {Object.entries(statuses).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select>
      </div>
      {editing && (
        <div className="space-y-4 px-4 pb-4" onKeyDown={(event) => {
          if (event.key === "Escape" && !busy) { event.stopPropagation(); closeEditor(); }
        }}>
          <form className="space-y-3" onSubmit={save}>
            <label className="block">
              <span className="eyebrow mb-1 block">Title</span>
              <input autoFocus required className="field w-full" value={title} disabled={busy}
                onChange={(event) => setTitle(event.target.value)} />
            </label>
            <label className="block">
              <span className="eyebrow mb-1 block">Notes</span>
              <textarea className="field w-full resize-y" rows={2} value={notes} disabled={busy}
                onChange={(event) => setNotes(event.target.value)} />
            </label>
            <label className="block">
              <span className="eyebrow mb-1 block">Parent task</span>
              <select className="field w-full" value={parentId} disabled={busy}
                onChange={(event) => setParentId(event.target.value)}>
                <option value="">No parent</option>
                {tasks.filter((candidate) => canParent(candidate, task.id, tasks)).map((candidate) => (
                  <option key={candidate.id} value={candidate.id}>{candidate.title}</option>
                ))}
              </select>
            </label>
            <div className="flex gap-2">
              <button className="btn btn-primary btn-sm" disabled={busy || !title.trim()}>Save</button>
              <button type="button" className="btn btn-quiet btn-sm" disabled={busy} onClick={closeEditor}>Close</button>
            </div>
          </form>
          <form className="flex gap-2" onSubmit={addSubtask}>
            <input ref={subtaskInput} className="field min-w-0 flex-1" aria-label={`New subtask for ${task.title}`}
              placeholder="Add a subtask…" required value={subtask} disabled={busy}
              onChange={(event) => setSubtask(event.target.value)} />
            <button className="btn btn-ghost" disabled={busy || !subtask.trim()}>Add subtask</button>
          </form>
          <section aria-label={`History for ${task.title}`} className="border-t border-edge pt-4">
            <h3 className="section-title mb-2">History</h3>
            {historyError ? <div role="alert" className="text-sm text-danger">{historyError}
              <button className="btn btn-quiet btn-sm" onClick={() => setHistoryRetry((value) => value + 1)}>Retry</button>
            </div> : history === null ? <p role="status" className="text-sm text-muted">Loading history…</p> :
              history.length === 0 ? <p className="text-sm text-muted">No changes recorded yet.</p> : (
                <ol className="space-y-2 text-sm">
                  {history.map((entry) => <li key={entry.id} className="[overflow-wrap:anywhere]">
                    <time dateTime={entry.timestamp} className="mr-2 text-xs text-muted">{new Date(entry.timestamp).toLocaleString()}</time>
                    {entry.field === "create" ? "Created task" : <span>
                      {entry.field === "parent_id" ? "Parent" : entry.field[0].toUpperCase() + entry.field.slice(1)}:
                      {" "}{historyValue(entry, entry.old_value)} → {historyValue(entry, entry.new_value)}
                    </span>}
                  </li>)}
                </ol>
              )}
          </section>
        </div>
      )}
      {error && <p role="alert" className="px-4 pb-4 text-sm text-danger">{error}</p>}
    </li>
  );
}
