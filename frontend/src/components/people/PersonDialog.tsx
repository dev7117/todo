import { useEffect, useState } from "react";
import { Trash2 } from "lucide-react";
import { useNavigate } from "react-router-dom";
import Modal from "../Modal";
import { type Area, type Person, useAgentMutations, useAgentProfile, useAgents, useCustomers, usePeopleMutations } from "../../lib/api";
import { useFocus } from "../../lib/focus";
import { useToast } from "../../hooks/useToast";

export default function PersonDialog({ person, kind: initialKind = "human", onClose }: { person?: Person; kind?: Person["kind"]; onClose: () => void }) {
  const { create, update, remove } = usePeopleMutations();
  const { save: saveAgent } = useAgentMutations();
  const { data: machines = [] } = useAgents();
  const [kind, setKind] = useState<Person["kind"]>(person?.kind ?? initialKind);
  const { data: profile } = useAgentProfile(person?.kind === "agent" ? person.id : null);
  const [agent, setAgent] = useState<AgentForm>({ executor: "claude", claude_agent: "", machine: "", model: "", allowed_tools: "", auto_dispatch: true });
  useEffect(() => {
    if (profile)
      setAgent({
        executor: profile.executor ?? "claude",
        claude_agent: profile.claude_agent ?? "",
        machine: profile.machine ?? "",
        model: profile.model ?? "",
        allowed_tools: profile.allowed_tools,
        auto_dispatch: profile.auto_dispatch,
      });
  }, [profile]);
  const isAgent = kind === "agent";
  const { data: customers = [] } = useCustomers();
  const { area } = useFocus();
  const { toast } = useToast();
  const navigate = useNavigate();
  const [form, setForm] = useState({
    name: person?.name ?? "",
    email: person?.email ?? "",
    title: person?.title ?? "",
    customer_id: person?.customer_id ?? null,
    area: person?.area ?? area ?? ("work" as Area),
    notes: person?.notes ?? "",
  });
  const [confirmDelete, setConfirmDelete] = useState(false);
  const set = (key: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) =>
    setForm({ ...form, [key]: e.target.value });

  return (
    <Modal title={person ? `Edit ${person.name}` : isAgent ? "New agent" : "New person"} onClose={onClose}>
      <form
        className="space-y-4"
        onSubmit={(e) => {
          e.preventDefault();
          const body = isAgent
            ? { name: form.name, title: form.title, notes: form.notes, kind, email: null, customer_id: null, area: "work" as Area }
            : { ...form, email: form.email || null, kind };
          const fail = (err: Error) => toast(err.message, "error");
          // An agent's profile saves after the person, so a new agent has an id to hang it on.
          const then = (p: Person, after: () => void) =>
            isAgent ? saveAgent.mutate({ personId: p.id, ...agent }, { onSuccess: after, onError: fail }) : after();
          if (person) update.mutate({ id: person.id, ...body }, { onSuccess: (p) => then(p, onClose), onError: fail });
          else create.mutate(body, { onSuccess: (p) => then(p, () => { onClose(); navigate(`/people/${p.id}`); }), onError: fail });
        }}
      >
        {!person && (
          <div className="segmented">
            {(["human", "agent"] as const).map((k) => (
              <button key={k} type="button" className="filter-tab" aria-pressed={kind === k} onClick={() => setKind(k)}>
                {k === "human" ? "Person" : "Agent"}
              </button>
            ))}
          </div>
        )}
        <div className="grid gap-3 sm:grid-cols-2">
          <label className="block">
            <span className="eyebrow mb-1.5 block">Name</span>
            <input className="field w-full" value={form.name} onChange={set("name")} required autoFocus />
          </label>
          <label className="block">
            <span className="eyebrow mb-1.5 block">Role</span>
            <input className="field w-full" placeholder={isAgent ? "Repo maintainer" : "Head of IT"} value={form.title} onChange={set("title")} />
          </label>
        </div>
        {isAgent ? (
          <AgentFields agent={agent} setAgent={setAgent} machines={machines.map((m) => m.name)} />
        ) : (
          <>
            <label className="block">
              <span className="eyebrow mb-1.5 block">Email</span>
              <input className="field w-full" type="email" placeholder="Used to spot them in meeting attendees" value={form.email} onChange={set("email")} />
            </label>
            <label className="block">
              <span className="eyebrow mb-1.5 block">Works at</span>
              <select
                className="field w-full"
                value={form.customer_id ?? ""}
                onChange={(e) => setForm({ ...form, customer_id: e.target.value ? Number(e.target.value) : null })}
              >
                <option value="">Our side (team, partners)</option>
                {customers.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.name}
                  </option>
                ))}
              </select>
              <span className="mt-1 block text-xs text-muted">Where they work. Tasks with them can span any customer or project.</span>
            </label>
            {!form.customer_id && (
              <div>
                <span className="eyebrow mb-1.5 block">Area</span>
                <div className="segmented">
                  {(["work", "personal"] as Area[]).map((a) => (
                    <button key={a} type="button" className="filter-tab" aria-pressed={form.area === a} onClick={() => setForm({ ...form, area: a })}>
                      {a === "work" ? "Work" : "Personal"}
                    </button>
                  ))}
                </div>
              </div>
            )}
          </>
        )}
        <label className="block">
          <span className="eyebrow mb-1.5 block">About</span>
          <textarea className="field min-h-[72px] w-full resize-y" placeholder="Context worth remembering" value={form.notes} onChange={set("notes")} />
        </label>
        <div className="flex flex-wrap items-center gap-2 pt-2">
          {person && (
            <button
              type="button"
              className="btn btn-danger btn-sm"
              onClick={() => {
                if (!confirmDelete) {
                  setConfirmDelete(true);
                  window.setTimeout(() => setConfirmDelete(false), 3000);
                  return;
                }
                remove.mutate(person.id, { onSuccess: () => { onClose(); navigate("/people"); } });
              }}
            >
              <Trash2 size={14} /> {confirmDelete ? "Click again (their tasks come back to you)" : "Delete"}
            </button>
          )}
          <div className="ml-auto flex gap-2">
            <button type="button" className="btn btn-ghost" onClick={onClose}>
              Cancel
            </button>
            <button type="submit" className="btn btn-primary" disabled={!form.name.trim()}>
              {person ? "Save" : isAgent ? "Add agent" : "Add person"}
            </button>
          </div>
        </div>
      </form>
    </Modal>
  );
}

type AgentForm = { executor: "claude" | "codex-herdr" | "codex-app"; claude_agent: string; machine: string; model: string; allowed_tools: string; auto_dispatch: boolean };

/** How an agent runs: the Claude Code agent file in the repo, where, and with which tools. */
function AgentFields({ agent, setAgent, machines }: { agent: AgentForm; setAgent: (a: AgentForm) => void; machines: string[] }) {
  const set = (key: keyof AgentForm) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
    setAgent({ ...agent, [key]: e.target.value });
  return (
    <>
      <label className="block">
        <span className="eyebrow mb-1.5 block">Run with</span>
        <select className="field w-full" value={agent.executor} onChange={(e) => setAgent({ ...agent, executor: e.target.value as AgentForm["executor"], allowed_tools: "" })}>
          <option value="claude">Claude Code</option>
          <option value="codex-herdr">Codex in Herdr</option>
          <option value="codex-app">Codex app / Remote</option>
        </select>
        {agent.executor === "codex-herdr" && <span className="mt-1 block text-xs text-muted">Available across projects. Reuses the repository’s Herdr workspace; general tasks use a scratch folder.</span>}
      {agent.executor === "codex-app" && <span className="mt-1 block text-xs text-muted">Runs on your already connected computer in Codex / ChatGPT Remote. Pair the computer in the app first.</span>}
        <span className="mt-1 block text-xs text-muted">One agent can handle any customer or project. Use About for a specialized role.</span>
      </label>
      <div className="grid gap-3 sm:grid-cols-2">
        {agent.executor === "claude" && <label className="block">
          <span className="eyebrow mb-1.5 block">Claude agent</span>
          <input className="field w-full font-mono text-[0.8125rem]" placeholder="maintainer" value={agent.claude_agent} onChange={set("claude_agent")} required />
          <span className="mt-1 block text-xs text-muted">.claude/agents/&lt;name&gt;.md: in the project's repo, or ~/.claude/agents on its machine for tasks without a repo</span>
        </label>}
        <label className="block">
          <span className="eyebrow mb-1.5 block">Runs on</span>
          <select className="field w-full" value={agent.machine} onChange={set("machine")}>
            <option value="">Whichever machine is online</option>
            {machines.map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
          </select>
        </label>
      </div>
      {agent.executor === "claude" && <label className="block">
        <span className="eyebrow mb-1.5 block">Allowed tools</span>
        <input
          className="field w-full font-mono text-[0.8125rem]"
          placeholder="Read Edit Write Bash(git *) Bash(gh pr *) Bash(npm *)"
          value={agent.allowed_tools}
          onChange={set("allowed_tools")}
        />
        <span className="mt-1 block text-xs text-muted">What it may do without asking (claude --allowedTools). It edits files in its worktree either way.</span>
      </label>}
      <div className="grid items-end gap-3 sm:grid-cols-2">
        <label className="block">
          <span className="eyebrow mb-1.5 block">Model</span>
          <input className="field w-full" placeholder={agent.executor === "claude" ? "From the agent file" : "Codex default"} value={agent.model} onChange={set("model")} />
        </label>
        <label className="flex items-center gap-2 pb-2 text-sm">
          <input type="checkbox" checked={agent.auto_dispatch} onChange={(e) => setAgent({ ...agent, auto_dispatch: e.target.checked })} />
          Start as soon as a task is assigned
        </label>
      </div>
      <p className="text-xs text-muted">
        {agent.executor === "codex-herdr" ? "Codex reads AGENTS.md and asks for permissions in its Herdr terminal." : agent.executor === "codex-app" ? "Codex reads AGENTS.md. Review permission requests in the native app or Remote." : <>Easiest from Claude Code in the repo: run <code>/mcp__todo__setup_agent</code> and it sets up both sides.</>}
      </p>
    </>
  );
}
