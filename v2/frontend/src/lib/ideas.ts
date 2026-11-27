import type { Block } from "./blocks";

export const ideaStatuses = { seed: "Seed", exploring: "Exploring", parked: "Parked", promoted: "Promoted", dropped: "Dropped" };
export type IdeaStatus = keyof typeof ideaStatuses;
export interface Idea {
  id: number;
  title: string;
  summary: string;
  status: IdeaStatus;
  project_id: number | null;
}
export interface IdeaDetail extends Idea { blocks: Block[] }

export async function api<T>(path: string, method = "GET", body?: object): Promise<T> {
  const response = await fetch(`/api${path}`, {
    method, headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!response.ok) throw new Error("Could not save or load this item. Please retry.");
  return response.status === 204 ? undefined as T : response.json();
}
