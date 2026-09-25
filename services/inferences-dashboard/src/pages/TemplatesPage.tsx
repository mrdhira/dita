import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useFieldArray, useForm } from "react-hook-form";
import { api, type Template } from "../api/client";
import { ErrorBanner } from "../components/ErrorBanner";
import { MAX_QUESTIONS, QUESTION_TYPES } from "../contract/schema";
import { usability } from "../lib/usability";
import { fromDraft, templateResolver, toDraft, type TemplateForm } from "./templateForm";

const blankQuestion: TemplateForm["questions"][number] = {
  name: "",
  type: "choice",
  optionsText: "",
  min: "",
  max: "",
  criteria: "",
};
const blank: TemplateForm = { name: "", description: "", questions: [blankQuestion] };

/**
 * The store is append-only, so a bad version cannot be deleted: retiring it is the way to stop
 * Decide offering it. Retiring is asked twice because nothing in the dashboard undoes it.
 */
function Versions({ name }: { name: string }) {
  const client = useQueryClient();
  const versions = useQuery({ queryKey: ["versions", name], queryFn: () => api.versions(name) });
  const [confirming, setConfirming] = useState<number | null>(null);
  const retire = useMutation({
    mutationFn: (version: number) => api.retire(name, version),
    onSuccess: () => {
      setConfirming(null);
      void client.invalidateQueries({ queryKey: ["templates"] });
      void client.invalidateQueries({ queryKey: ["versions", name] });
    },
  });
  return (
    <section aria-label={`versions of ${name}`} className="mt-6">
      <h3 className="mb-2 text-sm font-semibold">Versions of {name}</h3>
      {versions.error && <ErrorBanner error={versions.error} />}
      <ul className="space-y-1 text-sm">
        {versions.data?.versions.map((v) => (
          <li key={v.version} className="flex flex-wrap items-center gap-2">
            <span className={v.retired ? "text-slate-400 line-through" : ""}>v{v.version}</span>
            {v.retired ? (
              <span className="text-xs text-slate-500">retired</span>
            ) : confirming === v.version ? (
              <span className="flex flex-wrap items-center gap-2 text-xs">
                <span>Decide will no longer offer v{v.version}. Its predictions stay.</span>
                <button
                  type="button"
                  disabled={retire.isPending}
                  className="rounded-md bg-red-800 px-2 py-1 text-white disabled:opacity-40"
                  onClick={() => {
                    retire.mutate(v.version);
                  }}
                >
                  Retire v{v.version}
                </button>
                <button
                  type="button"
                  className="text-slate-600"
                  onClick={() => {
                    setConfirming(null);
                  }}
                >
                  cancel
                </button>
              </span>
            ) : (
              <button
                type="button"
                className="text-xs text-slate-600 underline"
                onClick={() => {
                  retire.reset();
                  setConfirming(v.version);
                }}
              >
                retire v{v.version}
              </button>
            )}
          </li>
        ))}
      </ul>
      {retire.error && <ErrorBanner error={retire.error} />}
    </section>
  );
}

/** Saving always writes a new version; an existing version is never edited in place. */
export function TemplatesPage() {
  const client = useQueryClient();
  const templates = useQuery({ queryKey: ["templates"], queryFn: api.templates });
  const [editing, setEditing] = useState<string | null>(null);
  const [loaded, setLoaded] = useState<string | null>(null);
  const [outdated, setOutdated] = useState<{ runs: boolean; items: string[] } | null>(null);
  const form = useForm<TemplateForm>({ resolver: templateResolver, defaultValues: blank });
  const questions = useFieldArray({ control: form.control, name: "questions" });
  const save = useMutation({
    mutationFn: (values: TemplateForm) => api.saveTemplate(toDraft(values)),
    onSuccess: (t) => {
      void client.invalidateQueries({ queryKey: ["templates"] });
      setEditing(`${t.name} v${t.version} saved`);
      setLoaded(t.name);
      setOutdated(null);
      form.reset(fromDraft(t));
    },
  });
  const errors = form.formState.errors;

  const load = (t: Template) => {
    form.reset(fromDraft(t));
    setLoaded(t.name);
    setEditing(`editing ${t.name}: saving writes v${t.version + 1}`);
    // An old version still runs; marking its fields now says what the next version must fix.
    const { usable, authoring } = usability(t);
    const items = authoring.map((f) => `${f.path}: ${f.message}`);
    setOutdated(items.length > 0 ? { runs: usable, items } : null);
    if (items.length > 0) void form.trigger();
  };

  return (
    <div className="grid gap-8 md:grid-cols-[1fr_2fr]">
      <section aria-label="templates">
        <h2 className="mb-2 text-sm font-semibold">Templates</h2>
        {templates.error && <ErrorBanner error={templates.error} />}
        <ul className="space-y-1 text-sm">
          {templates.data?.templates.map((t) => (
            <li key={t.name}>
              <button
                type="button"
                className="text-indigo-700"
                onClick={() => {
                  load(t);
                }}
              >
                {t.name} v{t.version}
              </button>
              {t.retired && <span className="ml-2 text-xs text-slate-500">retired</span>}
            </li>
          ))}
        </ul>
        <button
          type="button"
          className="mt-3 text-sm text-slate-600 underline"
          onClick={() => {
            form.reset(blank);
            setEditing(null);
            setLoaded(null);
            setOutdated(null);
          }}
        >
          new template
        </button>
        {loaded && <Versions name={loaded} />}
      </section>
      <form
        aria-label="template editor"
        className="space-y-4"
        onSubmit={(e) => {
          void form.handleSubmit((values) => {
            save.mutate(values);
          })(e);
        }}
      >
        {editing && <p className="text-xs text-slate-600">{editing}</p>}
        {outdated && (
          <div role="status" className="rounded-md border border-amber-300 bg-amber-50 p-3 text-sm">
            <p className="font-semibold text-amber-900">
              {outdated.runs
                ? "This version runs on Decide, but needs updating before the next one can be saved:"
                : "This version cannot be used on Decide; the next one must fix:"}
            </p>
            <ul className="mt-1 list-disc pl-5 text-amber-900">
              {outdated.items.map((o) => (
                <li key={o}>{o}</li>
              ))}
            </ul>
          </div>
        )}
        <label className="block text-sm font-medium">
          Name
          <input
            {...form.register("name")}
            className="mt-1 block w-full rounded-md border border-slate-300 p-2 font-mono text-sm"
          />
          {errors.name && <span className="text-xs text-red-700">{errors.name.message}</span>}
        </label>
        <label className="block text-sm font-medium">
          Description
          <input
            {...form.register("description")}
            className="mt-1 block w-full rounded-md border border-slate-300 p-2 text-sm"
          />
        </label>
        {questions.fields.map((field, i) => {
          const qe = errors.questions?.[i];
          return (
            <fieldset key={field.id} className="space-y-2 rounded-md border border-slate-200 p-3">
              <legend className="px-1 text-xs text-slate-500">question {i + 1}</legend>
              <div className="flex gap-2">
                <input
                  aria-label={`question ${i + 1} name`}
                  {...form.register(`questions.${i}.name`)}
                  placeholder="name"
                  className="flex-1 rounded-md border border-slate-300 p-2 font-mono text-sm"
                />
                <select
                  aria-label={`question ${i + 1} type`}
                  {...form.register(`questions.${i}.type`)}
                  className="rounded-md border border-slate-300 p-2 text-sm"
                >
                  {QUESTION_TYPES.map((t) => (
                    <option key={t} value={t}>
                      {t}
                    </option>
                  ))}
                </select>
              </div>
              {qe?.name && <span className="text-xs text-red-700">{qe.name.message}</span>}
              <label className="block text-sm font-medium">
                Instructions for the model: what this question asks, in the words the model reads
                (criteria)
                <textarea
                  {...form.register(`questions.${i}.criteria`)}
                  rows={2}
                  aria-describedby={`question-${i}-criteria-help`}
                  className="mt-1 block w-full rounded-md border border-slate-300 p-2 text-sm font-normal"
                />
              </label>
              <p id={`question-${i}-criteria-help`} className="text-xs text-slate-500">
                Required. The model reads this, not the question&apos;s name, and its answer moves
                with it: e.g. &ldquo;How severe is this homelab alert?&rdquo;
              </p>
              {qe?.criteria && <span className="text-xs text-red-700">{qe.criteria.message}</span>}
              <textarea
                aria-label={`question ${i + 1} options`}
                {...form.register(`questions.${i}.optionsText`)}
                rows={3}
                placeholder="one option per line (2-20)"
                className="block w-full rounded-md border border-slate-300 p-2 font-mono text-sm"
              />
              {qe?.optionsText && (
                <span className="text-xs text-red-700">{qe.optionsText.message}</span>
              )}
              <div className="flex gap-2 text-sm">
                <input
                  aria-label={`question ${i + 1} range min`}
                  {...form.register(`questions.${i}.min`)}
                  placeholder="range min (score)"
                  className="w-40 rounded-md border border-slate-300 p-2"
                />
                <input
                  aria-label={`question ${i + 1} range max`}
                  {...form.register(`questions.${i}.max`)}
                  placeholder="range max (score)"
                  className="w-40 rounded-md border border-slate-300 p-2"
                />
                <button
                  type="button"
                  className="ml-auto text-xs text-slate-600"
                  onClick={() => {
                    questions.remove(i);
                  }}
                >
                  remove
                </button>
              </div>
              {qe?.min && <span className="text-xs text-red-700">{qe.min.message}</span>}
            </fieldset>
          );
        })}
        {errors.questions?.root && (
          <p className="text-xs text-red-700">{errors.questions.root.message}</p>
        )}
        <div className="flex gap-3">
          <button
            type="button"
            disabled={questions.fields.length >= MAX_QUESTIONS}
            className="rounded-md border border-slate-300 px-3 py-2 text-sm disabled:opacity-40"
            onClick={() => {
              questions.append(blankQuestion);
            }}
          >
            add question
          </button>
          <button
            type="submit"
            disabled={save.isPending}
            className="rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-40"
          >
            Save as a new version
          </button>
        </div>
        {save.error && <ErrorBanner error={save.error} />}
      </form>
    </div>
  );
}
