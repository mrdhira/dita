import { useQuery } from "@tanstack/react-query";
import { Suspense } from "react";
import { NavLink, Outlet } from "react-router";
import { api } from "../api/client";
import { describeState, type Tone } from "../lib/fleet";

const sections = [
  { to: "/", label: "Fleet" },
  { to: "/models", label: "Models" },
  { to: "/activity", label: "Activity" },
  { to: "/jobs", label: "Jobs" },
  { to: "/settings", label: "Settings" },
];

const workbench = [
  { to: "/decide", label: "Decide" },
  { to: "/decisions", label: "History" },
  { to: "/templates", label: "Templates" },
  { to: "/eval", label: "Eval" },
];

function Links({ label, links }: { label: string; links: typeof sections }) {
  return (
    <nav aria-label={label} className="flex flex-wrap gap-4 text-sm">
      {links.map((l) => (
        <NavLink
          key={l.to}
          to={l.to}
          end={l.to === "/"}
          className={({ isActive }) =>
            `inline-block min-h-6 ${isActive ? "font-semibold text-indigo-700" : "text-slate-700"}`
          }
        >
          {l.label}
        </NavLink>
      ))}
    </nav>
  );
}

const strip: Record<Tone, string> = {
  good: "bg-emerald-100 text-emerald-900",
  warn: "bg-amber-100 text-amber-900",
  bad: "bg-red-100 text-red-900",
  idle: "bg-slate-100 text-slate-800",
};

/** A worker that is not running is a state to show, not a failure that breaks the page. */
function WorkerStrip() {
  const workers = useQuery({
    queryKey: ["workers"],
    queryFn: api.workers,
    refetchInterval: 30_000,
  });
  if (workers.isError) return <p className="text-xs text-slate-500">worker status unavailable</p>;
  return (
    <ul aria-label="workers" className="flex flex-wrap gap-2 text-xs">
      {workers.data?.workers.map((w) => {
        const view = describeState(w);
        return (
          <li
            key={w.name}
            title={w.error ?? w.url}
            data-tone={view.tone}
            className={`rounded px-2 py-0.5 ${strip[view.tone]}`}
          >
            {w.name}: <span aria-hidden="true">{view.shape}</span> {view.word}
          </li>
        );
      })}
    </ul>
  );
}

export function Layout() {
  return (
    <div className="mx-auto max-w-5xl p-6">
      <header className="mb-6 flex flex-wrap items-center justify-between gap-4 border-b border-slate-200 pb-4">
        <div>
          <h1 className="text-lg font-semibold">Inferences dashboard</h1>
          <p className="text-xs text-slate-600">
            Observe only · this console loads, unloads and restarts nothing
          </p>
        </div>
        <div className="flex flex-col items-end gap-1 max-sm:items-start">
          <Links label="sections" links={sections} />
          <div className="flex flex-wrap items-baseline gap-2 text-xs text-slate-600">
            decision workbench
            <Links label="decision workbench" links={workbench} />
          </div>
        </div>
      </header>
      <WorkerStrip />
      <main className="mt-6">
        <Suspense fallback={<p className="text-sm text-slate-600">Loading this page…</p>}>
          <Outlet />
        </Suspense>
      </main>
    </div>
  );
}
