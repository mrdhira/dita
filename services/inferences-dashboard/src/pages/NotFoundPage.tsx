import { Link, useLocation } from "react-router";

export function NotFoundPage() {
  const { pathname } = useLocation();
  return (
    <section aria-label="no such page" className="space-y-3">
      <h2 className="text-sm font-semibold">There is no page at {pathname}</h2>
      <p className="text-sm text-slate-600">
        Past predictions are under History, at <span className="font-mono">/decisions</span>.
      </p>
      <Link to="/decide" className="text-sm text-indigo-700">
        Back to Decide
      </Link>
    </section>
  );
}
