import { useQuery } from "@tanstack/react-query";
import { useParams } from "react-router";
import { api } from "../api/client";
import { AnswerView } from "../components/AnswerView";
import { CorrectionForm, RecordedCorrection } from "../components/CorrectionForm";
import { ErrorBanner } from "../components/ErrorBanner";

/** One prediction and its correction, read back from the store: what a reload shows. */
export function DecisionPage() {
  const { id = "" } = useParams();
  const decision = useQuery({ queryKey: ["decision", id], queryFn: () => api.decision(id) });
  if (decision.error) return <ErrorBanner error={decision.error} />;
  if (!decision.data) return <p className="text-sm text-slate-500">Loading…</p>;
  const d = decision.data;
  return (
    <article className="space-y-6">
      <header className="text-sm text-slate-600">
        <p>
          Prediction <span className="font-mono">{d.id}</span> · {d.schema.name} v{d.schema.version}
        </p>
        <p>
          Model <span className="font-mono">{d.model_id}</span> revision{" "}
          <span className="font-mono">{d.model_revision}</span> ·{" "}
          {new Date(d.created_at).toLocaleString()}
        </p>
      </header>
      <blockquote className="whitespace-pre-wrap rounded-md bg-slate-100 p-3 font-mono text-sm">
        {d.input_text}
      </blockquote>
      <div className="grid gap-4 md:grid-cols-2">
        {d.answers.map((a) => (
          <AnswerView key={a.question} answer={a} />
        ))}
      </div>
      {d.correction ? <RecordedCorrection decision={d} /> : <CorrectionForm decision={d} />}
    </article>
  );
}
