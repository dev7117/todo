import { useEffect, useState } from "react";

export default function FromV1() {
  const [items, setItems] = useState<string[] | null>(null);
  useEffect(() => {
    let live = true;
    fetch("/api/from-v1")
      .then((response) => response.ok ? response.json() : null)
      .then((body: { enabled: boolean; items: string[] } | null) => {
        if (live && body?.enabled) setItems(body.items);
      })
      .catch(() => {});
    return () => { live = false; };
  }, []);

  if (items === null) return null;
  return (
    <section className="well mt-6" aria-labelledby="from-v1-title">
      <header className="p-4">
        <h2 id="from-v1-title" className="section-title">From v1</h2>
        <p className="mt-1 text-sm text-muted">Open tasks from your saved v1 database. Read only.</p>
      </header>
      {items.length ? (
        <ul className="border-t border-edge">
          {items.map((title, index) => <li key={index} className="border-b border-edge p-4 last:border-b-0 [overflow-wrap:anywhere]">{title}</li>)}
        </ul>
      ) : <p className="px-4 pb-4 text-sm text-muted">No open tasks in this snapshot.</p>}
    </section>
  );
}
