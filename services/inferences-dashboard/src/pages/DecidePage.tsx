import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useNavigate } from "react-router";
import { api, type Template } from "../api/client";
import { ErrorBanner } from "../components/ErrorBanner";
import { MAX_TEXT, chars, decisionRequestSchema, issuePaths } from "../contract/schema";
import { usability, type Usability } from "../lib/usability";

const key = (t: Template) => `${t.name}@${t.version}`;

const faultText = (faults: { path: string; message: string }[]) =>
  faults.map((f) => `${f.path}: ${f.message}`).join("; ");

function label(t: Template, u: Usability): string {
  const head = `${t.name} v${t.version}`;
  if (t.retired) return `${head} — retired`;
  if (!u.usable) {
    return `${head} — cannot be used: ${u.faults[0]?.message ?? "the orchestrator will not run it"}`;
  }
  const size = `${head} (${t.questions.length} questions)`;
  return u.authoring.length > 0 ? `${size} — runs, but needs updating` : size;
}

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

  // One the runtime refuses stays listed with its reason, but is never the default and never sent.
  const list = (templates.data?.templates ?? []).map((t) => ({ t, u: usability(t) }));
  const usable = list.filter(({ t, u }) => !t.retired && u.usable);
  const chosen =
    list.find(({ t }) => !t.retired && key(t) === picked) ?? (picked ? undefined : usable[0]);

  return (
    <form
      aria-label="run a decision"
      className="space-y-4"
      onSubmit={(e) => {
        e.preventDefault();
        if (!chosen) return;
        const { t, u } = chosen;
        if (!u.usable) {
          setClientIssues([
            `${t.name} v${t.version} cannot be used: ${faultText(u.faults) || "the orchestrator will not run it"}`,
          ]);
          return;
        }
        const body = { text, schema: { name: t.name, version: t.version } };
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
          value={chosen ? key(chosen.t) : ""}
          onChange={(e) => {
            setPicked(e.target.value);
          }}
          className="mt-1 block rounded-md border border-slate-300 p-2 text-sm"
        >
          {!chosen && <option value="">choose a template</option>}
          {list.map(({ t, u }) => (
            <option key={key(t)} value={key(t)} disabled={t.retired}>
              {label(t, u)}
            </option>
          ))}
        </select>
      </label>
      {chosen && chosen.u.usable && chosen.u.authoring.length > 0 && (
        <p className="text-sm text-amber-900">
          {chosen.t.name} v{chosen.t.version} runs, but was saved before today&apos;s editing rules:{" "}
          {faultText(chosen.u.authoring)}. To bring it up to date, load it under{" "}
          <Link to="/templates" className="text-indigo-700 underline">
            Templates
          </Link>{" "}
          and save the next version.
        </p>
      )}
      {list.length === 0 && templates.isSuccess && (
        <p className="text-sm text-slate-600">
          No templates yet: define one under Templates first.
        </p>
      )}
      {list.length > 0 && usable.length === 0 && (
        <p className="text-sm text-slate-600">
          None of these templates can be used: save a new version of one under Templates.
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
