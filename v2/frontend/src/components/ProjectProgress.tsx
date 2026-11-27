import type { Progress } from "../lib/projects";

export default function ProjectProgress({ progress, title }: { progress: Progress; title: string }) {
  const label = progress.total === 0 ? "Nothing linked yet" : `${progress.done} of ${progress.total} tasks done`;
  return <div className="space-y-2">
    <div className="flex justify-between gap-3 text-sm text-muted">
      <span>{label}</span>
      {progress.total > 0 && <span className="tabular">{Math.round(progress.percent)}%</span>}
    </div>
    <div role="progressbar" aria-label={`Progress for ${title}`} aria-valuenow={progress.percent}
      aria-valuemin={0} aria-valuemax={100} aria-valuetext={label}
      className="h-2 overflow-hidden rounded-full bg-fg/10">
      <div className="h-full rounded-full bg-accent" style={{ width: `${progress.percent}%` }} />
    </div>
  </div>;
}
