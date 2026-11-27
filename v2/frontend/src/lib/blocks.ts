export interface Block {
  id: number;
  kind: "note" | "subtask";
  body: string;
  body_html: string;
  position: number;
  done: boolean;
  done_at: string | null;
}
