import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useNavigate } from "react-router";
import { api } from "../api/client";
import { ErrorBanner } from "../components/ErrorBanner";
import { MAX_TEXT, chars, decisionRequestSchema, issuePaths } from "../contract/schema";

export function DecidePage() {
  const navigate = useNavigate();
  const templates = useQuery({ queryKey: ["templates"], queryFn: api.templates });
  const [text, setText] = useState("");
  const [picked, setPicked] = useState("");
  const [clientIssues, setClientIssues] = useState<string[]>([]);
  const decide = useMutation({
    mutationFn: (body: { text: string; schema: { name: string; version: number } }) =>
      api.decide(body.text, body.schema),
    onSuccess: (d) => {
      void navigate(`/decisions/${d.id}`);
    },
  });

  const list = templates.data?.templates ?? [];
  const chosen = list.find((t) => `${t.name}@${t.version}` === picked) ?? list[0];

  return (
    <form
      aria-label="run a decision"
      className="space-y-4"
      onSubmit={(e) => {
        e.preventDefault();
        if (!chosen) return;
        const body = { text, schema: { name: chosen.name, version: chosen.version } };
        const parsed = decisionRequestSchema.safeParse(body);
        setClientIssues(parsed.success ? [] : issuePaths(parsed.error));
        if (parsed.success) decide.mutate(body);
      }}
    >
      <label className="block text-sm font-medium">
        State text
        <textarea
          name="text"
          value={text}
          onChange={(e) => {
            setText(e.target.value);
          }}
          rows={8}
          className="mt-1 block w-full rounded-md border border-slate-300 p-2 font-mono text-sm"
          placeholder="Paste the state to decide on."
        />
        <span className="text-xs text-slate-500">
          {chars(text)} / {MAX_TEXT} · pasted text only in v1
        </span>
      </label>
      <label className="block text-sm font-medium">
        Schema template
        <select
          name="template"
          value={chosen ? `${chosen.name}@${chosen.version}` : ""}
          onChange={(e) => {
            setPicked(e.target.value);
          }}
          className="mt-1 block rounded-md border border-slate-300 p-2 text-sm"
        >
          {list.map((t) => (
            <option key={t.name} value={`${t.name}@${t.version}`}>
              {t.name} v{t.version} ({t.questions.length} questions)
            </option>
          ))}
        </select>
      </label>
      {list.length === 0 && templates.isSuccess && (
        <p className="text-sm text-slate-600">
          No templates yet: define one under Templates first.
        </p>
      )}
      {templates.error && <ErrorBanner error={templates.error} />}
      {clientIssues.length > 0 && (
        <p role="alert" className="text-sm text-red-800">
          Fix before sending: {clientIssues.join(", ")}
        </p>
      )}
      {decide.error && <ErrorBanner error={decide.error} />}
      <button
        type="submit"
        disabled={!chosen || decide.isPending}
        className="rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-40"
      >
        {decide.isPending ? "Asking the worker…" : "Decide"}
      </button>
    </form>
  );
}
