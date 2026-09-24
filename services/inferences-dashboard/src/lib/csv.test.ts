import { describe, expect, it } from "vitest";
import { CsvError, parseLabelledCsv } from "./csv";

describe("parseLabelledCsv", () => {
  it.each([
    [
      "a plain file",
      "label,p:a,p:b\na,0.9,0.1\nb,0.2,0.8\n",
      [
        { label: "a", probabilities: { a: 0.9, b: 0.1 } },
        { label: "b", probabilities: { a: 0.2, b: 0.8 } },
      ],
    ],
    [
      "CRLF, a BOM, a trailing blank line and an ignored column",
      "﻿id,label,p:low,p:high\r\n1,low,0.6,0.4\r\n\r\n",
      [{ label: "low", probabilities: { low: 0.6, high: 0.4 } }],
    ],
    [
      "a quoted label with a comma and an escaped quote",
      'label,p:x,p:y\n"say ""hi"", then",0.5,0.5\n',
      [{ label: 'say "hi", then', probabilities: { x: 0.5, y: 0.5 } }],
    ],
  ])("reads %s", (_, text, rows) => {
    expect(parseLabelledCsv(text)).toEqual(rows);
  });

  it.each([
    ["an empty file", "", "empty"],
    ["no label column", "p:a,p:b\n0.5,0.5\n", "no `label` column"],
    ["one option column", "label,p:a\na,1\n", "fewer than two"],
    ["a header only", "label,p:a,p:b\n", "no rows"],
    ["a short row", "label,p:a,p:b\na,0.5\n", "line 2 has 2 fields"],
    [
      "a probability that is not a number",
      "label,p:a,p:b\na,high,0.5\n",
      "line 2: p:a is not a number",
    ],
    ["an unclosed quote", 'label,p:a,p:b\n"a,0.5,0.5\n', "never closed"],
  ])("refuses %s", (_, text, message) => {
    expect(() => parseLabelledCsv(text)).toThrow(CsvError);
    expect(() => parseLabelledCsv(text)).toThrow(message);
  });
});
