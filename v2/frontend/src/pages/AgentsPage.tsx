import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowUpRight, GitPullRequest, CircleDot, RefreshCw } from "lucide-react";

export interface AgentItem {
  repo: string;
  number: number;
  kind: "issue" | "pr";
  title: string;
  url: string;
  author: string;
  updated_at: string;
  state: string;
  ci: "absent" | "green" | "running" | "failing" | "unknown";
  waiting_on_dev: boolean;
  prs: AgentItem[];
}
interface AgentWork {
  configured: boolean;
  last_poll_at: string | null;
  last_updated_at: string | null;
  next_refresh_at: string | null;
  last_error: string | null;
  waiting_on_dev: AgentItem[];
  repos: { repo: string; items: AgentItem[] }[];
  recent: AgentItem[];
}

/** Read the server cache on either page so the navigation count stays current. */
export function useAgentWork() {
  const [data, setData] = useState<AgentWork | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [now, setNow] = useState(Date.now());
  const inFlight = useRef(false);
  const live = useRef(false);
  const load = useCallback(async (refresh = false) => {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    try {
      const response = await fetch(`/api/agent-work${refresh ? "?refresh=true" : ""}`);
      if (!response.ok) throw new Error("Could not load agent work. Please try again.");
      const body: AgentWork = await response.json();
      if (live.current) { setData(body); setError(""); }
    } catch {
      if (live.current) setError("Could not load agent work. Please try again.");
    } finally {
      inFlight.current = false;
      if (live.current) { setBusy(false); setNow(Date.now()); }
    }
  }, []);
  useEffect(() => {
    live.current = true;
    void load();
    const poll = window.setInterval(() => void load(), 30_000);
    const tick = window.setInterval(() => setNow(Date.now()), 1_000);
    return () => { live.current = false; clearInterval(poll); clearInterval(tick); };
  }, [load]);
  return { data, error, busy, now, load };
}

function ago(value: string | null, now: number) {
  if (!value) return "Not yet updated";
  const minutes = Math.max(0, Math.floor((now - Date.parse(value)) / 60_000));
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m ago`;
  if (minutes < 1440) return `${Math.floor(minutes / 60)}h ago`;
  return `${Math.floor(minutes / 1440)}d ago`;
}

const ciColors = { absent: "bg-faint", green: "bg-success", running: "bg-warning", failing: "bg-danger", unknown: "bg-faint" };

function Row({ item, now }: { item: AgentItem; now: number }) {
  const Icon = item.kind === "pr" ? GitPullRequest : CircleDot;
  return (
    <a href={item.url} target="_blank" rel="noopener noreferrer"
      className="flex items-start gap-3 rounded-lg p-4 hover:bg-fg/[0.03] [overflow-wrap:anywhere]">
      <Icon size={16} aria-label={item.kind === "pr" ? "Pull request" : "Issue"} className="mt-1 shrink-0 text-muted" />
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <span className={`tag ${item.state === "CI failing" ? "text-danger" : ""}`}>{item.state}</span>
          <span className="tabular text-xs text-muted">{item.repo} #{item.number}</span>
        </div>
        <p className="mt-1 font-medium">{item.title}</p>
        <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted">
          <span className="inline-flex items-center gap-1.5" title={`CI: ${item.ci}`}>
            <span className={`h-1.5 w-1.5 rounded-full ${ciColors[item.ci]}`} aria-hidden="true" />
            CI {item.ci === "absent" ? "—" : item.ci}
          </span>
          <span>{item.author}</span>
          <time dateTime={item.updated_at} title={item.updated_at}>{ago(item.updated_at, now)}</time>
        </div>
      </div>
      <ArrowUpRight size={14} className="mt-1 shrink-0 text-faint" aria-hidden="true" />
      <span className="sr-only">Opens in a new tab</span>
    </a>
  );
}

function Rows({ items, now }: { items: AgentItem[]; now: number }) {
  return <ul>{items.map((item) => (
    <li key={`${item.repo}-${item.number}`} className="border-b border-edge last:border-b-0">
      <Row item={item} now={now} />
      {item.prs.length > 0 && <ul aria-label={`Pull requests for issue ${item.number}`} className="mb-3 ml-7 mr-3 border-l border-edge sm:ml-10">
        {item.prs.map((pr) => <li key={pr.number}><Row item={pr} now={now} /></li>)}
      </ul>}
    </li>
  ))}</ul>;
}

export default function AgentsPage({ work }: { work: ReturnType<typeof useAgentWork> }) {
  const { data, error, busy, now, load } = work;
  const cooldown = data?.next_refresh_at ? Math.max(0, Math.ceil((Date.parse(data.next_refresh_at) - now) / 1000)) : 0;
  return (
    <div className="anim-fade">
      <header className="mb-8 flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="page-title">Agents</h1>
          <p className="mt-1 text-muted">Coding work across your GitHub repos.</p>
          {data?.configured && <p className="mt-2 text-xs text-faint" role="status">Last updated {ago(data.last_updated_at, now)}</p>}
        </div>
        <div className="text-right">
          <button className="btn btn-ghost" disabled={busy || data?.configured === false || cooldown > 0}
            onClick={() => void load(true)}><RefreshCw size={14} />{busy ? "Refreshing…" : "Refresh"}</button>
          {cooldown > 0 && <p className="mt-1 text-xs text-faint">Available in {cooldown}s</p>}
        </div>
      </header>
      {(error || data?.last_error) && <div role="alert" className="well mb-6 p-4 text-sm text-danger">
        {error || data?.last_error}
        {data?.last_updated_at && <p className="mt-1 text-muted">Showing the last successful update.</p>}
      </div>}
      {!data ? <p role="status" className="text-muted">{error ? "Agent work is unavailable." : "Loading agent work…"}</p> : !data.configured ? (
        <section className="well px-6 py-16 text-center">
          <h2 className="section-title">GitHub not configured</h2>
          <p className="mt-2 text-sm text-muted">Connect GitHub with a read-only token and repo allowlist in the server settings.</p>
        </section>
      ) : <>
        <section aria-labelledby="waiting-title" className="mb-8">
          <h2 id="waiting-title" className="section-title mb-3 flex items-center gap-2">Waiting on you <span className="tag tabular">{data.waiting_on_dev.length}</span></h2>
          <div className="well">
            {data.waiting_on_dev.length ? <Rows items={data.waiting_on_dev} now={now} /> : <p className="p-5 text-sm text-muted">Nothing waiting on you.</p>}
          </div>
        </section>
        <div className="space-y-8">
          {data.repos.map((group) => <section key={group.repo} aria-label={group.repo}>
            <h2 className="section-title mb-3">{group.repo}</h2>
            <div className="well">
              {group.items.length ? <Rows items={group.items} now={now} /> : <p className="p-5 text-sm text-muted">No open issues or pull requests.</p>}
            </div>
          </section>)}
        </div>
        <details className="well mt-8">
          <summary className="cursor-pointer p-4 font-medium">Recently merged / closed <span className="ml-1 text-muted">({data.recent.length})</span><span className="ml-3 text-xs font-normal text-faint">Last 7 days</span></summary>
          <div className="border-t border-edge"><Rows items={data.recent} now={now} />
            {!data.recent.length && <p className="p-5 text-sm text-muted">No recently completed work.</p>}
          </div>
        </details>
      </>}
    </div>
  );
}
