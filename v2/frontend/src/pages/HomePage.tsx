import { Target } from "lucide-react";

/** Home will be the goals view (PRD §5). Until goals exist it says so, with no sample data. */
export default function HomePage() {
  return (
    <div className="anim-fade">
      <header className="mb-8">
        <h1 className="page-title">Home</h1>
        <p className="mt-1 text-muted">Your goals and the projects that move them.</p>
      </header>
      <section className="well flex flex-col items-center px-6 py-16 text-center">
        <div className="mb-3 grid h-10 w-10 place-items-center rounded-full bg-fg/[0.06] text-muted">
          <Target size={18} />
        </div>
        <p className="font-medium">No goals yet</p>
        <p className="mt-1 max-w-sm text-sm text-muted">
          Goals, and the projects and tasks under them, will show up here once they exist.
        </p>
      </section>
    </div>
  );
}
