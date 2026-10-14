import { useState } from "react";
import { Bot, ChevronRight, ExternalLink, GitBranch, Play, Send, Square } from "lucide-react";
import { Link } from "react-router-dom";
import { ACTIVE_RUN, isClosed, type LauncherRun, type Task, useAllPeople, useAgentMutations, useTaskRuns } from "../../lib/api";
import { ago } from "../../lib/format";
import { useToast } from "../../hooks/useToast";
import { RunTag } from "../hub/RunHistory";

const OUTCOME: Record<NonNullable<LauncherRun["outcome"]>, string> = {
  review: "Asked for review",
  question: "Asked you a question",
  ready: "Setup check passed",
};

/** A task assigned to an agent: what it's doing right now, its log, and a way to answer it. */
export default function AgentPanel({ task }: { task: Task }) {
  const { data: people = [] } = useAllPeople();
  const { data: runs = [] } = useTaskRuns(task.id);
  const { dispatch, stop } = useAgentMutations();
  const { toast } = useToast();
  const [reply, setReply] = useState("");
  const [showLog, setShowLog] = useState(false);
  const assignee = people.find((p) => p.id === task.assignee_id);
  const isAgent = assignee?.kind === "agent";
  if (!isAgent && !runs.length) return null;

  const latest = runs[0];
  const active = latest && ACTIVE_RUN.includes(latest.status);
  const fail = { onError: (e: Error) => toast(e.message, "error") };
  const waitingOnYou = task.status === "waiting" && !active;
  const name = assignee?.name ?? latest?.person ?? "Agent";
  const tokens = runs.reduce((sum, r) => sum + (r.tokens ?? 0), 0);
  const cached = runs.reduce((sum, r) => sum + (r.cached_tokens ?? 0), 0);
  const send = () =>
    dispatch.mutate({ taskId: task.id, message: reply.trim() || undefined }, { ...fail, onSuccess: () => setReply("") });

  return (
    <section className="well mb-8 p-4" aria-label="Agent">
      <div className="flex flex-wrap items-center gap-2">
        <span className="inline-grid h-7 w-7 place-items-center rounded-full bg-accent/10 text-accent">
          <Bot size={15} />
        </span>
        <Link to={`/people/${assignee?.id ?? latest?.person_id}`} className="font-medium hover:underline">
          {name}
        </Link>
        {latest ? <RunTag run={latest} /> : <span className="tag">Not started</span>}
        {latest?.outcome && !active && <span className="tag tag-accent">{OUTCOME[latest.outcome]}</span>}
        <div className="ml-auto flex items-center gap-1.5">
          {active ? (
            <button type="button" className="btn btn-ghost btn-sm" onClick={() => stop.mutate(latest.id, fail)} disabled={stop.isPending}>
              <Square size={13} /> Stop
            </button>
          ) : (
            isAgent &&
            !isClosed(task.status) &&
            !waitingOnYou && (
              <button type="button" className="btn btn-ghost btn-sm" onClick={send} disabled={dispatch.isPending}>
                <Play size={13} /> {latest ? "Run again" : "Start"}
              </button>
            )
          )}
        </div>
      </div>

      {latest && (
        <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 pl-9 text-xs text-muted">
          <span>{latest.agent}</span>
          {latest.launch?.session && <span>Herdr: {latest.launch.session} · {latest.launch.workspace} · {latest.launch.pane_id}</span>}
          {latest.launch?.thread_id && <span>Codex Remote: {latest.launch.remote_server} · {latest.launch.thread_id}</span>}
          {latest.launch?.status === "blocked" && <span className="text-warning">Needs your input in the selected harness</span>}
          {["needs_inspection", "cancel_pending"].includes(latest.launch?.status ?? "") && <span className="text-warning">Inspect the recorded task before starting again</span>}
          {latest.branch && (
            <span className="inline-flex items-center gap-1">
              <GitBranch size={12} /> {latest.branch}
            </span>
          )}
          {runs.find((r) => r.pr_url)?.pr_url && (
            <a href={runs.find((r) => r.pr_url)!.pr_url!} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-accent hover:underline">
              <ExternalLink size={12} /> Pull request
            </a>
          )}
          {tokens > 0 && (
            <span
              className="tabular"
              title={`${tokens.toLocaleString()} tokens (new input + output)${cached ? `, plus ${cached.toLocaleString()} read from cache` : ""}${runs.length > 1 ? `, over ${runs.length} runs` : ""}`}
            >
              {compact(tokens)} tokens
            </span>
          )}
          <span className="tabular">{ago(latest.requested_at)}</span>
          {latest.error && <span className="text-danger">{latest.error}</span>}
        </div>
      )}

      {latest && (
        <div className="mt-3 pl-9">
          <button
            type="button"
            className="inline-flex items-center gap-1 text-xs text-muted hover:text-fg"
            aria-expanded={showLog}
            onClick={() => setShowLog(!showLog)}
          >
            <ChevronRight size={12} className={`transition-transform duration-200 ease-out ${showLog ? "rotate-90" : ""}`} />
            Log{runs.length > 1 ? ` (latest of ${runs.length} runs)` : ""}
          </button>
          {showLog &&
            (latest.output ? (
              <pre className="diff anim-fade mt-2 max-h-80 overflow-auto whitespace-pre-wrap rounded-[8px] border border-edge bg-fg/[0.03] p-3 text-xs">
                {latest.output}
              </pre>
            ) : (
              <p className="mt-1 text-xs text-faint">{active ? "No output yet…" : "No output."}</p>
            ))}
        </div>
      )}

      {isAgent && waitingOnYou && !isClosed(task.status) && (
        <div className="mt-4 pl-9">
          <textarea
            className="field min-h-[64px] w-full resize-y text-sm"
            placeholder={latest?.outcome === "question" ? `Answer ${name}…` : `Anything for ${name} before it goes again? (optional)`}
            value={reply}
            onChange={(e) => setReply(e.target.value)}
            onKeyDown={(e) => (e.metaKey || e.ctrlKey) && e.key === "Enter" && send()}
          />
          <div className="mt-2 flex justify-end">
            <button type="button" className="btn btn-primary btn-sm" onClick={send} disabled={dispatch.isPending || (latest?.outcome === "question" && !reply.trim())}>
              <Send size={13} /> Send to {name}
            </button>
          </div>
        </div>
      )}
    </section>
  );
}

/** 950 → "950", 2251 → "2.3k", 22512 → "23k", 1_250_000 → "1.3M" */
function compact(n: number) {
  if (n < 1000) return String(n);
  if (n < 1_000_000) return `${(n / 1000).toFixed(n < 10_000 ? 1 : 0).replace(/\.0$/, "")}k`;
  return `${(n / 1_000_000).toFixed(1).replace(/\.0$/, "")}M`;
}
