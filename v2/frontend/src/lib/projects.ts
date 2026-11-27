import type { Task } from "./tasks";

export const projectStatuses = { active: "Active", paused: "Paused", done: "Done", archived: "Archived" };
export type ProjectStatus = keyof typeof projectStatuses;
export interface Progress { done: number; total: number; percent: number }
export interface Project {
  id: number;
  title: string;
  description: string;
  status: ProjectStatus;
  target_on: string | null;
  created_at: string;
  updated_at: string;
  progress: Progress;
}
export interface ProjectDetail extends Project { tasks: Task[] }

export async function projectRequest<T>(path = "", method = "GET", body?: object): Promise<T> {
  const response = await fetch(`/api/projects${path}`, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!response.ok) throw new Error(response.status === 404 ? "Project not found." : "Could not save or load projects. Please try again.");
  return response.json();
}
