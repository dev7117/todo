import { useEffect, useState } from "react";

interface Hit { kind: "task" | "idea" | "block"; id: number; title: string; excerpt: string; url: string }
interface Results { hits: Hit[]; limit: number }

export default function SearchPage({ query }: { query: string }) {
  const [results, setResults] = useState<Results | null>(null);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setResults(null); setError("");
    fetch(`/api/search?q=${encodeURIComponent(query)}`, { signal: controller.signal })
      .then((response) => { if (!response.ok) throw new Error("Could not search. Please try again."); return response.json(); })
      .then(setResults)
      .catch((error: Error) => { if (!controller.signal.aborted) setError(error.message); });
    return () => controller.abort();
  }, [query, retry]);
  return <div className="anim-fade">
    <header className="mb-8">
      <h1 className="page-title">Search</h1>
      <p className="mt-1 text-muted [overflow-wrap:anywhere]">{query ? <>Results for “{query}”</> : "Find a task, idea, or note."}</p>
    </header>
    {error ? <div role="alert" className="well p-4"><p className="text-danger">{error}</p>
      <button className="btn btn-ghost btn-sm mt-2" onClick={() => setRetry((value) => value + 1)}>Retry</button></div>
      : results === null ? <p role="status" className="text-muted">Searching…</p>
      : results.hits.length ? <>
        <p role="status" className="mb-3 text-sm text-muted">{results.hits.length === results.limit ? `Showing the first ${results.limit} matches. Refine your search for more.` : `${results.hits.length} matches`}</p>
        <ul className="well divide-y divide-edge">{results.hits.map((hit) => <li key={`${hit.kind}-${hit.id}`}>
          <a data-app-link href={hit.url} className="block p-5 hover:bg-panel [overflow-wrap:anywhere]">
            <span className="eyebrow">{hit.kind === "block" ? "Note / checklist" : hit.kind}</span>
            <h2 className="mt-1 font-medium">{hit.title}</h2>
            {hit.excerpt && <p className="mt-2 whitespace-pre-wrap text-sm text-muted">{hit.excerpt}</p>}
          </a>
        </li>)}</ul>
      </> : <div className="well px-6 py-16 text-center" role="status">
        <h2 className="font-medium">{query ? "No matches found" : "Start with a few words"}</h2>
        <p className="mt-2 text-sm text-muted">{query ? "Try a different phrase. Search covers task titles and notes, ideas, and note blocks." : "Use the search field above to find something you saved."}</p>
      </div>}
  </div>;
}
