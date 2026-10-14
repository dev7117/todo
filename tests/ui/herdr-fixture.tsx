/** Render the real affected components with local cached fixtures; never reach an API. */
import React from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import "@fontsource-variable/inter";
import PersonDialog from "../../frontend/src/components/people/PersonDialog";
import AgentPanel from "../../frontend/src/components/task/AgentPanel";
import type { Person, Task } from "../../frontend/src/lib/api";

const params = new URLSearchParams(location.search);
const view = params.get("view") ?? "form";
const executor = params.get("executor") === "codex-app" ? "codex-app" : "codex-herdr";
const state = params.get("state") ?? "blocked";
document.documentElement.classList.toggle("dark", params.get("theme") === "dark");
const requests: string[] = [];
window.fetch = async (input) => {
  requests.push(String(input));
  throw new Error("QA fixture forbids network and mutation calls");
};
const errors: string[] = [];
window.addEventListener("error", (e) => errors.push(e.message));
window.addEventListener("unhandledrejection", () => errors.push("unhandled rejection"));
const person = { id: 1, name: "Codex", title: "Repository maintainer", kind: "agent", area: "work", notes: "", archived: false } as Person;
const task = { id: 1, title: "Implement assigned task in Herdr", assignee_id: 1, status: state === "blocked" ? "in_progress" : "waiting", area: "work" } as Task;
const client = new QueryClient({ defaultOptions: { queries: { staleTime: Infinity, retry: false, refetchOnWindowFocus: false } } });
client.setQueryData(["agents"], [{ name: "Omarchy fixture", online: true, platform: "linux", version: "1.4" }]);
client.setQueryData(["customers", false], []);
client.setQueryData(["people", "all"], [person]);
client.setQueryData(["agent-profile", 1], { ...person, executor, machine: "Omarchy fixture", claude_agent: "codex", model: "", allowed_tools: "", auto_dispatch: true, configured: true });
client.setQueryData(["runs", "task", 1], [{
  id: 1, kind: "task", executor, agent: "Omarchy fixture", person: "Codex", person_id: 1,
  task_id: 1, label: "Codex: Implement assigned task in Herdr", status: state === "blocked" ? "running" : state === "cancel_pending" ? "cancelled" : "failed", exit_code: null, started_at: null, finished_at: null,
  requested_at: "2026-10-14T15:00:00Z", branch: "agent/1-implement-assigned-task", output: "[Codex in Herdr default / wA / wA:p9]", tokens: 0,
  error: state === "needs_inspection" ? "Launch needs inspection; no automatic retry" : null,
  launch: executor === "codex-app" ? { route: "codex-app", thread_id: "thread-fixture", remote_server: "Fixture computer", status: state } : { session: "default", workspace: "wA", tab: "wA:t9", pane_id: "wA:p9", status: state },
}]);
createRoot(document.getElementById("root")!).render(
  <QueryClientProvider client={client}>
    <MemoryRouter>
      {view === "form" ? <PersonDialog person={person} onClose={() => {}} /> :
        <main style={{ maxWidth: 960, margin: "40px auto", padding: 16 }}><h1 className="mb-4 text-xl">Task agent</h1><AgentPanel task={task} /></main>}
    </MemoryRouter>
  </QueryClientProvider>
);
const pause = () => new Promise((resolve) => setTimeout(resolve, 40));
setTimeout(async () => {
  const checks: Record<string, boolean> = {};
  if (view === "form") {
    const select = Array.from(document.querySelectorAll("select")).find((el) => el.querySelector('option[value="codex-herdr"]'))!;
    checks.codexSelected = select?.value === executor;
    checks.claudeFieldsHidden = !document.body.textContent?.includes("Claude agent") && !document.body.textContent?.includes("Allowed tools");
    select.value = "claude";
    select.dispatchEvent(new Event("change", { bubbles: true }));
    await pause();
    checks.switchBackShowsClaudeFields = !!document.body.textContent?.includes("Claude agent") && !!document.body.textContent?.includes("Allowed tools");
    select.value = executor;
    select.dispatchEvent(new Event("change", { bubbles: true }));
    await pause();
    checks.switchToCodexHidesClaudeFields = !document.body.textContent?.includes("Claude agent") && !document.body.textContent?.includes("Allowed tools");
    checks.machineVisible = !!document.body.textContent?.includes("Omarchy fixture");
    const dialog = document.querySelector('[role="dialog"]')!;
    const rect = dialog.getBoundingClientRect();
    checks.dialogInsideViewport = rect.left >= 0 && rect.right <= innerWidth && rect.top >= 0 && rect.bottom <= innerHeight;
    const beforeScroll = dialog.scrollTop;
    dialog.scrollTop = dialog.scrollHeight;
    await pause();
    const save = Array.from(dialog.querySelectorAll("button")).find((el) => el.textContent === "Save")!;
    const saveRect = save.getBoundingClientRect();
    checks.footerReachableByScroll = saveRect.top >= rect.top && saveRect.bottom <= rect.bottom;
    if (params.get("scroll") !== "bottom") dialog.scrollTop = beforeScroll;
  } else {
    checks.locationVisible = !!document.body.textContent?.includes(executor === "codex-app" ? "Codex Remote: Fixture computer · thread-fixture" : "Herdr: default · wA · wA:p9");
    checks.statusVisible = !!document.body.textContent?.includes(state === "blocked" ? "Needs your input in the selected harness" : "Inspect the recorded task before starting again");
  }
  checks.noUndefinedLabels = !document.body.textContent?.includes("undefined");
  checks.noHorizontalOverflow = document.documentElement.scrollWidth <= innerWidth;
  checks.noRequests = requests.length === 0;
  checks.noBrowserErrors = errors.length === 0;
  document.documentElement.dataset.qa = JSON.stringify({ view, state, checks, pass: Object.values(checks).every(Boolean) });
}, 200);
