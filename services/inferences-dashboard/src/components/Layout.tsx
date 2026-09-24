import { useQuery } from "@tanstack/react-query";
import { NavLink, Outlet } from "react-router";
import { api } from "../api/client";

const links = [
  { to: "/decide", label: "Decide" },
  { to: "/decisions", label: "History" },
  { to: "/templates", label: "Templates" },
  { to: "/eval", label: "Eval" },
];

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
      {workers.data?.workers.map((w) => (
        <li
          key={w.name}
          title={w.error ?? w.url}
          className={`rounded px-2 py-0.5 ${w.state === "ready" ? "bg-emerald-100 text-emerald-900" : "bg-amber-100 text-amber-900"}`}
        >
          {w.name}: {w.state.replace("_", " ")}
        </li>
      ))}
    </ul>
  );
}

export function Layout() {
  return (
    <div className="mx-auto max-w-5xl p-6">
      <header className="mb-6 flex flex-wrap items-center justify-between gap-4 border-b border-slate-200 pb-4">
        <div>
          <h1 className="text-lg font-semibold">Inferences dashboard</h1>
          <p className="text-xs text-slate-500">
            Suggestions only · every answer is recorded as a prediction and correction pair
          </p>
        </div>
        <nav className="flex gap-4 text-sm">
          {links.map((l) => (
            <NavLink
              key={l.to}
              to={l.to}
              className={({ isActive }) =>
                isActive ? "font-semibold text-indigo-700" : "text-slate-600"
              }
            >
              {l.label}
            </NavLink>
          ))}
        </nav>
      </header>
      <WorkerStrip />
      <main className="mt-6">
        <Outlet />
      </main>
    </div>
  );
}
