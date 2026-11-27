export const statuses = {
  inbox: "Inbox", todo: "To do", in_progress: "In progress",
  waiting: "Waiting", done: "Done", cancelled: "Cancelled",
};
export type Status = keyof typeof statuses;
export interface Task {
  id: number;
  title: string;
  notes: string;
  status: Status;
  parent_id: number | null;
  project_id: number | null;
  updated_at: string;
  today_on: string | null;
  is_today: boolean;
}
export interface HistoryEntry {
  id: number;
  timestamp: string;
  field: string;
  old_value: string | null;
  new_value: string | null;
}
export const isFinished = (task: Task) => task.status === "done" || task.status === "cancelled";

export async function request<T>(path = "", method = "GET", body?: object): Promise<T> {
  const response = await fetch(`/api/tasks${path}`, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!response.ok) throw new Error("Could not save or load tasks. Please try again.");
  return response.json();
}

// Partition first, then nest within each section. A child with a finished parent
// remains visible among active tasks, with its parent named on the row.
export function taskTree(tasks: Task[]): { task: Task; depth: number }[] {
  const ids = new Set(tasks.map((task) => task.id));
  const children = new Map<number | null, Task[]>();
  for (const task of [...tasks].sort((a, b) => a.id - b.id)) {
    const parent = task.parent_id !== null && ids.has(task.parent_id) ? task.parent_id : null;
    children.set(parent, [...(children.get(parent) ?? []), task]);
  }
  const rows: { task: Task; depth: number }[] = [];
  const stack = (children.get(null) ?? []).map((task) => ({ task, depth: 0 })).reverse();
  while (stack.length) {
    const row = stack.pop()!;
    rows.push(row);
    for (const task of [...(children.get(row.task.id) ?? [])].reverse()) {
      stack.push({ task, depth: row.depth + 1 });
    }
  }
  return rows;
}

export function canParent(candidate: Task, taskId: number, tasks: Task[]): boolean {
  let ancestor: Task | undefined = candidate;
  while (ancestor) {
    if (ancestor.id === taskId) return false;
    const parentId: number | null = ancestor.parent_id;
    ancestor = tasks.find((task) => task.id === parentId);
  }
  return true;
}
