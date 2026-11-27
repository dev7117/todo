import { type FormEvent, useEffect, useRef, useState } from "react";

interface Item {
  id: number;
  title: string;
  notes: string;
  status: "open" | "done";
}

async function request<T>(path = "", method = "GET", body?: object): Promise<T> {
  const response = await fetch(`/api/items${path}`, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!response.ok) throw new Error("Could not save or load items. Please try again.");
  return response.json();
}

function ItemRow({ item, onChange }: { item: Item; onChange: (item: Item) => void }) {
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState(item.title);
  const [notes, setNotes] = useState(item.notes);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const editButton = useRef<HTMLButtonElement>(null);

  function closeEditor() {
    setEditing(false);
    // Restore keyboard focus once the title button has rendered again.
    requestAnimationFrame(() => editButton.current?.focus());
  }

  async function save(event: FormEvent) {
    event.preventDefault();
    if (busy || !title.trim()) return;
    setBusy(true);
    setError("");
    try {
      onChange(await request<Item>(`/${item.id}`, "PATCH", { title, notes }));
      closeEditor();
    } catch (error) {
      setError((error as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function toggle() {
    setBusy(true);
    setError("");
    try {
      onChange(await request<Item>(`/${item.id}/${item.status === "open" ? "complete" : "reopen"}`, "POST"));
    } catch (error) {
      setError((error as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <li className="border-b border-edge last:border-b-0">
      <div className="flex items-start gap-3 p-4">
        <input type="checkbox" className="mt-1 h-4 w-4 shrink-0 cursor-pointer"
          aria-label={`${item.status === "open" ? "Complete" : "Reopen"} ${item.title}`}
          checked={item.status === "done"} disabled={busy || editing} onChange={toggle} />
        {editing ? (
          <form className="min-w-0 flex-1 space-y-3" onSubmit={save}
            onKeyDown={(event) => { if (event.key === "Escape" && !busy) closeEditor(); }}>
            <label className="block">
              <span className="eyebrow mb-1 block">Title</span>
              <input autoFocus required className="field w-full" value={title} disabled={busy}
                onChange={(event) => setTitle(event.target.value)} />
            </label>
            <label className="block">
              <span className="eyebrow mb-1 block">Notes</span>
              <textarea className="field w-full resize-y" rows={3} value={notes} disabled={busy}
                onChange={(event) => setNotes(event.target.value)} />
            </label>
            <div className="flex gap-2">
              <button className="btn btn-primary btn-sm" disabled={busy || !title.trim()}>Save</button>
              <button type="button" className="btn btn-quiet btn-sm" disabled={busy} onClick={closeEditor}>Cancel</button>
            </div>
          </form>
        ) : (
          <button ref={editButton} type="button" className="min-w-0 flex-1 text-left [overflow-wrap:anywhere]"
            aria-label={`Edit ${item.title}`} disabled={busy} onClick={() => {
              setTitle(item.title); setNotes(item.notes); setError(""); setEditing(true);
            }}>
            <span className={item.status === "done" ? "text-muted line-through" : "font-medium"}>{item.title}</span>
            {item.notes && <span className="mt-1 block whitespace-pre-wrap text-sm text-muted">{item.notes}</span>}
          </button>
        )}
      </div>
      {error && <p role="alert" className="px-4 pb-4 text-sm text-danger">{error}</p>}
    </li>
  );
}

export default function HomePage() {
  const [items, setItems] = useState<Item[]>([]);
  const [title, setTitle] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [error, setError] = useState("");
  const addInput = useRef<HTMLInputElement>(null);

  async function load() {
    setLoading(true);
    setLoadError("");
    try { setItems(await request<Item[]>()); }
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
      const item = await request<Item>("", "POST", { title });
      setItems((current) => [...current, item]);
      setTitle("");
    } catch (error) { setError((error as Error).message); }
    finally {
      setBusy(false);
      requestAnimationFrame(() => addInput.current?.focus());
    }
  }

  const update = (item: Item) => setItems((current) => current.map((old) => old.id === item.id ? item : old));
  const open = items.filter((item) => item.status === "open");
  const done = items.filter((item) => item.status === "done");

  return (
    <div className="anim-fade">
      <header className="mb-8">
        <h1 className="page-title">Home</h1>
        <p className="mt-1 text-muted">A little room for what needs doing.</p>
      </header>
      <form className="mb-6 flex gap-2" onSubmit={add}>
        <input ref={addInput} className="field min-w-0 flex-1" aria-label="New item" placeholder="Add an item…"
          required value={title} disabled={busy || loading || !!loadError} onChange={(event) => setTitle(event.target.value)} />
        <button className="btn btn-primary" disabled={busy || loading || !!loadError || !title.trim()}>Add</button>
      </form>
      {error && <p role="alert" className="mb-4 text-sm text-danger">{error}</p>}
      {loading ? <p role="status" className="text-muted">Loading items…</p> : loadError ? (
        <div role="alert" className="well p-4">
          <p className="mb-2 text-danger">{loadError}</p>
          <button className="btn btn-ghost btn-sm" onClick={load}>Retry</button>
        </div>
      ) : (
        <>
          <section aria-label="Open items" className="well">
            {open.length ? <ul>{open.map((item) => <ItemRow key={item.id} item={item} onChange={update} />)}</ul> : (
              <div className="px-6 py-16 text-center">
                <p className="font-medium">No open items</p>
                <p className="mt-1 text-sm text-muted">Add something above when you’re ready.</p>
              </div>
            )}
          </section>
          {done.length > 0 && (
            <details className="well mt-6">
              <summary className="cursor-pointer p-4 font-medium">Done <span className="ml-1 text-muted">({done.length})</span></summary>
              <ul className="border-t border-edge">{done.map((item) => <ItemRow key={item.id} item={item} onChange={update} />)}</ul>
            </details>
          )}
        </>
      )}
    </div>
  );
}
