import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router";
import { api } from "../api/client";
import { ErrorBanner } from "../components/ErrorBanner";

export function HistoryPage() {
  const recent = useQuery({ queryKey: ["decisions"], queryFn: api.recent });
  if (recent.error) return <ErrorBanner error={recent.error} />;
  const list = recent.data?.decisions ?? [];
  if (recent.isSuccess && list.length === 0) {
    return <p className="text-sm text-slate-600">No predictions recorded yet.</p>;
  }
  return (
    <table className="w-full text-sm">
      <thead>
        <tr className="text-left text-xs text-slate-500">
          <th className="font-normal">when</th>
          <th className="font-normal">schema</th>
          <th className="font-normal">text</th>
          <th className="font-normal">answer</th>
        </tr>
      </thead>
      <tbody>
        {list.map((d) => (
          <tr key={d.id} className="border-t border-slate-100">
            <td className="py-1">
              <Link to={`/decisions/${d.id}`} className="text-indigo-700">
                {new Date(d.created_at).toLocaleString()}
              </Link>
            </td>
            <td>
              {d.schema.name} v{d.schema.version}
            </td>
            <td className="max-w-xs truncate font-mono">{d.input_text}</td>
            <td>{d.correction ? "recorded" : "awaiting a human"}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
