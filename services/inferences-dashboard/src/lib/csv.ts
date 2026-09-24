/**
 * The eval panel's input: one row per labelled example, a `label` column and one `p:<option>`
 * column per option holding the model's probability for it. RFC 4180 quoting is honoured.
 */
export interface LabelledRow {
  label: string;
  probabilities: Record<string, number>;
}

export class CsvError extends Error {}

function records(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let field = "";
  let quoted = false;
  for (let i = 0; i < text.length; i++) {
    const c = text.charAt(i);
    if (quoted) {
      if (c === '"' && text.charAt(i + 1) === '"') {
        field += '"';
        i++;
      } else if (c === '"') {
        quoted = false;
      } else {
        field += c;
      }
    } else if (c === '"' && field === "") {
      quoted = true;
    } else if (c === ",") {
      row.push(field);
      field = "";
    } else if (c === "\n" || c === "\r") {
      if (c === "\r" && text.charAt(i + 1) === "\n") i++;
      row.push(field);
      rows.push(row);
      row = [];
      field = "";
    } else {
      field += c;
    }
  }
  if (quoted) throw new CsvError("a quoted field is never closed");
  if (field !== "" || row.length > 0) {
    row.push(field);
    rows.push(row);
  }
  return rows.filter((r) => !(r.length === 1 && r[0] === ""));
}

export function parseLabelledCsv(text: string): LabelledRow[] {
  const [header, ...body] = records(text.replace(/^\uFEFF/, ""));
  if (!header) throw new CsvError("the file is empty");
  const labelAt = header.indexOf("label");
  if (labelAt < 0) throw new CsvError("there is no `label` column");
  const options = header
    .map((name, at) => ({ name, at }))
    .filter((c) => c.name.startsWith("p:"))
    .map((c) => ({ option: c.name.slice(2), at: c.at }));
  if (options.length < 2) throw new CsvError("there are fewer than two `p:<option>` columns");
  if (body.length === 0) throw new CsvError("there are no rows under the header");
  return body.map((fields, r) => {
    const line = r + 2;
    if (fields.length !== header.length) {
      throw new CsvError(
        `line ${line} has ${fields.length} fields, the header has ${header.length}`,
      );
    }
    const probabilities: Record<string, number> = {};
    for (const { option, at } of options) {
      const raw = (fields[at] ?? "").trim();
      const p = Number(raw);
      if (raw === "" || !Number.isFinite(p)) {
        throw new CsvError(`line ${line}: p:${option} is not a number`);
      }
      probabilities[option] = p;
    }
    return { label: (fields[labelAt] ?? "").trim(), probabilities };
  });
}
