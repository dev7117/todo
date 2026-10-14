import { useMemo, useState } from "react";
import { Bot, Check, CalendarClock, ChevronLeft, ChevronRight, Mail, Pencil, UsersRound, X } from "lucide-react";
import { Link } from "react-router-dom";
import { useParams } from "react-router-dom";
import QuickAdd from "../components/QuickAdd";
import { EmptyState } from "../components/TaskList";
import TaskRow from "../components/TaskRow";
import { useOpenMeeting } from "../components/MeetingDialog";
import Notebook from "../components/notebook/Notebook";
import Avatar from "../components/people/Avatar";
import PersonDialog from "../components/people/PersonDialog";
import { type Person, type PersonView, type Task, useAgentCheck, useAgentMutations, usePerson, useRun } from "../lib/api";
import { RunTag } from "../components/hub/RunHistory";
import { useToast } from "../hooks/useToast";
import { meetingWhen } from "../lib/format";

/** One person, built for a 1:1: what to follow up on, what they're on, what you share. */
export default function PersonPage() {
  const id = Number(useParams().personId);
  const { data: view, error } = usePerson(id);
  if (error) return <EmptyState icon={<UsersRound size={18} />} title="Person not found" />;
  if (!view) return null;
  return <PersonDetail key={id} view={view} />;
}

function PersonDetail({ view }: { view: PersonView }) {
  const { person, counts } = view;
  const [editing, setEditing] = useState(false);
  const [showDone, setShowDone] = useState(false);
  const plusName = person.name.toLowerCase().replace(/\s+/g, "-");

  // Their tasks, grouped by customer · project.
  const groups = useMemo(() => {
    const map = new Map<string, Task[]>();
    for (const t of view.assigned) {
      const key = [t.customer, t.project].filter(Boolean).join(" · ") || "No project";
      map.set(key, [...(map.get(key) ?? []), t]);
    }
    return [...map.entries()];
  }, [view.assigned]);

  return (
    <div>
      <Link to="/people" className="btn btn-quiet btn-xs -ml-2 mb-4">
        <ChevronLeft size={13} /> People
      </Link>

      <header className="mb-8 flex flex-wrap items-center gap-5">
        <Avatar name={person.name} size={72} agent={person.kind === "agent"} />
        <div className="min-w-0 flex-1">
          <h1 className="page-title">{person.name}</h1>
          <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-muted">
            {person.title && <span>{person.title}</span>}
            {person.kind === "agent" ? (
              <span className="inline-flex items-center gap-1">
                <Bot size={13} /> Agent
              </span>
            ) : person.customer ? (
              <Link to={`/customers/${person.customer_id}`} className="hover:text-fg">
                {person.customer}
              </Link>
            ) : (
              <span>Our side</span>
            )}
            {person.email && (
              <a href={`mailto:${person.email}`} className="inline-flex items-center gap-1 hover:text-fg">
                <Mail size={13} /> {person.email}
              </a>
            )}
          </div>
          <div className="mt-3 flex flex-wrap gap-1.5">
            <span className="tag tabular">{counts.assigned_open} on them</span>
            <span className="tag tabular">{counts.following_open} following</span>
            {counts.overdue > 0 && <span className="tag tag-danger tabular">{counts.overdue} overdue</span>}
            {counts.waiting > 0 && <span className="tag tag-warning tabular">{counts.waiting} waiting on them</span>}
            {counts.done_30d > 0 && <span className="tag tag-success tabular">{counts.done_30d} done in 30 days</span>}
          </div>
        </div>
        <button type="button" className="btn btn-ghost btn-sm" onClick={() => setEditing(true)}>
          <Pencil size={14} /> Edit
        </button>
      </header>

      <div className="grid gap-8 lg:grid-cols-[minmax(0,1fr)_320px]">
        <div className="min-w-0 space-y-10">
          <Section title="Follow up" count={view.follow_up.length} hint="Overdue, due this week, or waiting on them">
            {view.follow_up.length ? <Rows tasks={view.follow_up} personId={person.id} /> : <Quiet>Nothing needs chasing.</Quiet>}
          </Section>

          <Section title="1:1 notes">
            <Notebook
              owner={{ kind: "person", id: person.id }}
              blocks={view.blocks}
              documentTitle={person.name}
              starters={[{ title: "Next 1:1 agenda" }, { title: "Feedback" }, { title: "Goals" }, { title: "Running notes" }]}
              emptyTitle="Keep 1:1 notes here"
              emptyHint="An agenda, running notes, feedback, goals. Each block is its own small document."
            />
          </Section>

          <Section title="On them" count={view.assigned.length}>
            <div className="mb-4 max-w-2xl">
              <QuickAdd placeholder={`Assign something to ${person.name.split(" ")[0]}…  #project ^fri`} implied={`+${plusName}`} />
            </div>
            {groups.length ? (
              groups.map(([label, tasks]) => (
                <div key={label} className="mb-5">
                  <div className="eyebrow mb-1 px-2">{label}</div>
                  <Rows tasks={tasks} personId={person.id} />
                </div>
              ))
            ) : (
              <Quiet>Nothing assigned to them.</Quiet>
            )}
          </Section>

          <Section title="Following" count={view.following.length} hint="Yours; to discuss or keep them in the loop">
            {view.following.length ? <Rows tasks={view.following} personId={person.id} /> : <Quiet>@mention them in a task, or add them as a follower.</Quiet>}
          </Section>

          {view.done_recently.length > 0 && (
            <section>
              <button type="button" className="btn btn-quiet btn-sm -ml-3" aria-expanded={showDone} onClick={() => setShowDone(!showDone)}>
                <ChevronRight size={14} className={`transition-transform duration-200 ease-out ${showDone ? "rotate-90" : ""}`} />
                Done in the last 30 days <span className="tabular text-faint">{view.done_recently.length}</span>
              </button>
              {showDone && (
                <div className="anim-rise mt-2">
                  <Rows tasks={view.done_recently} personId={person.id} />
                </div>
              )}
            </section>
          )}
        </div>

        <aside className="min-w-0 space-y-6">
          {person.kind === "agent" && <AgentSetupCard person={person} />}
          {view.shared.length > 0 && (
            <section className="well p-4">
              <h2 className="eyebrow mb-2">Across</h2>
              <ul className="space-y-1 text-sm">
                {view.shared.map((s) => (
                  <li key={`${s.customer_id}-${s.project_id}`} className="flex items-baseline gap-2">
                    <span className="min-w-0 flex-1">
                      {s.customer && <span className="text-muted">{s.customer} · </span>}
                      {s.project_id ? (
                        <Link to={`/projects/${s.project_id}`} className="hover:underline">
                          {s.project}
                        </Link>
                      ) : (
                        "No project"
                      )}
                    </span>
                    <span className="tabular text-xs text-faint">{s.open}</span>
                  </li>
                ))}
              </ul>
            </section>
          )}
          <MeetingsCard view={view} />
          {person.notes && (
            <section className="well p-4">
              <h2 className="eyebrow mb-2">About</h2>
              <p className="whitespace-pre-wrap text-sm text-muted">{person.notes}</p>
            </section>
          )}
        </aside>
      </div>

      {editing && <PersonDialog person={person} onClose={() => setEditing(false)} />}
    </div>
  );
}

/** An agent's page: is it ready to work, and a smoke test that proves it end to end. */
function AgentSetupCard({ person }: { person: Person }) {
  const { data: check } = useAgentCheck(person.id);
  const { test } = useAgentMutations();
  const { toast } = useToast();
  const [runId, setRunId] = useState<number | null>(null);
  const { data: run } = useRun(runId);
  return (
    <section className="well p-4">
      <h2 className="eyebrow mb-2 flex items-center gap-1.5">
        <Bot size={13} /> Setup
      </h2>
      {check && (
        <ul className="space-y-1.5 text-sm">
          {check.checks.map((c) => (
            <li key={c.check} className="flex gap-2">
              {c.ok ? <Check size={14} className="mt-0.5 shrink-0 text-success" /> : <X size={14} className="mt-0.5 shrink-0 text-danger" />}
              <span className="min-w-0">
                <span className="font-medium">{c.check}</span> <span className="break-words text-muted">{c.detail}</span>
                {c.fix && <span className="block text-xs text-faint">{c.fix}</span>}
              </span>
            </li>
          ))}
        </ul>
      )}
      <div className="mt-3 flex items-center gap-2">
        <button
          type="button"
          className="btn btn-ghost btn-sm"
          disabled={test.isPending || check?.executor?.startsWith("codex-")}
          onClick={() => test.mutate(person.id, { onSuccess: (r) => setRunId(r.id), onError: (e) => toast(e.message, "error") })}
        >
          Run setup check
        </button>
        {run && <RunTag run={run} />}
      </div>
      {run?.output && (
        <pre className="diff anim-fade mt-2 max-h-60 overflow-auto whitespace-pre-wrap rounded-[8px] border border-edge bg-fg/[0.03] p-2 text-xs">{run.output}</pre>
      )}
      <p className="mt-3 text-xs text-muted">{check?.executor?.startsWith("codex-") ? "Codex handles tasks across customers and projects. A linked repo gets its own worktree; other tasks use a scratch folder. Configure the harness once on its machine." : "Assign it a task and it starts on its machine: in a worktree when the task’s project has a repo, else in a scratch folder."}</p>
    </section>
  );
}

function MeetingsCard({ view }: { view: PersonView }) {
  const open = useOpenMeeting();
  return (
    <section className="well p-4">
      <h2 className="eyebrow mb-2 flex items-center gap-1.5">
        <CalendarClock size={13} /> Meetings with them
      </h2>
      {view.meetings.length ? (
        <ul className="-mx-2">
          {view.meetings.map((m) => (
            <li key={m.id}>
              <button type="button" className="w-full rounded-md px-2 py-1.5 text-left text-sm hover:bg-fg/[0.05]" onClick={() => open(m.id)}>
                <div className="truncate font-medium">
                  {m.customer} · {m.title}
                </div>
                <div className="text-xs text-muted">
                  {meetingWhen(m)}
                  {m.status === "scheduled" ? " · upcoming" : ""}
                </div>
              </button>
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-sm text-faint">None found. Meetings match on their name or email in the attendee list.</p>
      )}
    </section>
  );
}

function Section({ title, count, hint, children }: { title: string; count?: number; hint?: string; children: React.ReactNode }) {
  return (
    <section>
      <div className="mb-2 flex flex-wrap items-baseline gap-2 px-1">
        <h2 className="section-title">{title}</h2>
        {count !== undefined && <span className="tabular text-sm text-faint">{count}</span>}
        {hint && <span className="text-xs text-faint">{hint}</span>}
      </div>
      {children}
    </section>
  );
}

function Rows({ tasks, personId }: { tasks: Task[]; personId: number }) {
  return (
    <div className="-mx-1">
      {tasks.map((t) => (
        <TaskRow key={t.id} task={t} hidePersonId={personId} />
      ))}
    </div>
  );
}

function Quiet({ children }: { children: React.ReactNode }) {
  return <p className="px-2 text-sm text-faint">{children}</p>;
}
