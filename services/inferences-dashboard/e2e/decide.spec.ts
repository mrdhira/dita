import { mkdirSync } from "node:fs";
import { expect, test, type Page } from "@playwright/test";
import type { Decision } from "../src/api/client";
import { STUB_LABELS, pinnedModel, realWorkerUrl } from "./worker";

// The scenario the requirement names, because it catches the likeliest bug: losing the pair.
// paste -> decide -> correct -> reload -> the correction is still attached to its prediction.
// It runs against the stub by default and against the real worker when E2E_SYSTEM_ONE_URL is set.

const real = realWorkerUrl !== undefined;
const mode = real ? "real" : "stubbed";
// The real worker takes seconds per question; the stub answers at once.
const answered = { timeout: real ? 150_000 : 5_000 };
test.describe.configure({ timeout: real ? 300_000 : 30_000 });

const evidence = "../../docs/inferences/dashboard/evidence";
mkdirSync(evidence, { recursive: true });

// Every screenshot that shows something other than a real answer says so on the page itself.
async function label(page: Page, text: string) {
  await page.evaluate((text) => {
    const label = document.createElement("div");
    label.textContent = text;
    Object.assign(label.style, {
      position: "fixed",
      top: "0",
      right: "0",
      zIndex: "9999",
      padding: "6px 10px",
      background: "#b91c1c",
      color: "#fff",
      font: "600 13px system-ui",
    });
    document.body.appendChild(label);
  }, text);
}

async function expectNoStubLabels(page: Page) {
  for (const stub of STUB_LABELS) await expect(page.locator("body")).not.toContainText(stub);
}

const template = {
  name: "alert-triage",
  description: "Alert triage, for the e2e",
  questions: [
    {
      name: "severity",
      type: "choice",
      options: ["low", "medium", "high", "critical"],
      criteria: "How severe is this alert?",
    },
    {
      name: "fraud",
      type: "noul",
      options: ["false", "true"],
      criteria: "Is this alert a sign of fraud?",
    },
    {
      name: "risk_score",
      type: "score",
      options: ["1", "2", "3", "4", "5"],
      range: { min: 1, max: 5 },
      criteria: "How risky is this alert, from 1 (none) to 5 (severe)?",
    },
  ],
};

test("paste, decide, correct, reload: the correction is still attached", async ({
  page,
  request,
}) => {
  const problems: string[] = [];
  page.on("console", (m) => {
    if (m.type() === "error") problems.push(m.text());
  });
  page.on("pageerror", (e) => problems.push(e.message));

  const pinned = real ? pinnedModel() : undefined;
  if (realWorkerUrl && pinned) {
    const info = (await (await request.get(`${realWorkerUrl}/info`)).json()) as Record<
      string,
      unknown
    >;
    expect(info, "the worker serves the model models.yaml pins").toMatchObject({
      model_id: pinned.id,
      model_revision: pinned.revision,
    });
  }

  const saved = await request.post("/api/inferences/schemas", { data: template });
  expect(saved.status()).toBe(201);

  await page.goto("/decide");
  await expect(page.getByRole("list", { name: "workers" })).toContainText(
    "inferences-system-one: ready",
  );
  await page
    .getByRole("textbox", { name: "State text" })
    .fill(
      "Alert 4411: three failed logins from a new device, then a transfer of 48,000,000 IDR to a first-time payee.",
    );
  await page.getByRole("combobox", { name: "Schema template" }).selectOption("alert-triage@1");
  await page.getByRole("button", { name: "Decide" }).click();

  await expect(page).toHaveURL(/\/decisions\/[0-9a-f]{24}$/, answered);
  const id = page.url().split("/").pop() ?? "";
  for (const question of ["severity", "fraud", "risk_score"]) {
    const card = page.getByRole("region", { name: `suggestion for ${question}` });
    await expect(card.getByTestId("top-1")).toContainText("%");
    await expect(card.getByTestId("top-2")).toContainText("%");
    await expect(card.getByTestId("confidence")).toContainText("%");
  }
  const predicted = (await (
    await request.get(`/api/inferences/decisions/${id}`)
  ).json()) as Decision;
  if (pinned) {
    expect(predicted.model_id).toBe(pinned.id);
    expect(predicted.model_revision).toBe(pinned.revision);
    await expect(page.getByText(pinned.revision)).toBeVisible();
    for (const a of predicted.answers ?? []) {
      const sum = a.options.reduce((s, o) => s + o.probability, 0);
      expect(Math.abs(sum - 1), `${a.question} sums to ${sum}`).toBeLessThanOrEqual(1e-6);
    }
    const fraudAnswer = predicted.answers?.find((a) => a.question === "fraud");
    expect(fraudAnswer?.options.map((o) => o.option).sort()).toEqual(["false", "true"]);
    await expectNoStubLabels(page);
  } else {
    await expect(page.getByText("STUB-not-a-model")).toBeVisible();
    await label(page, "STUBBED: worker is a stub, not a model");
  }
  await page.screenshot({ path: `${evidence}/1-answer-view-${mode}.png`, fullPage: true });

  const severityTop =
    (
      await page
        .getByRole("region", { name: "suggestion for severity" })
        .getByTestId("top-1")
        .textContent()
    )?.split(" · ")[0] ?? "";
  await page.getByRole("button", { name: `use suggestion (${severityTop})` }).click();
  const fraud = page.getByRole("group", { name: "fraud" });
  const fraudTop =
    (
      await page
        .getByRole("region", { name: "suggestion for fraud" })
        .getByTestId("top-1")
        .textContent()
    )?.split(" · ")[0] ?? "";
  const fraudOther = fraudTop === "true" ? "false" : "true";
  await fraud.getByRole("radio", { name: fraudOther }).check();
  await page
    .getByRole("button", { name: /use suggestion/ })
    .last()
    .click();
  await page.getByRole("button", { name: "Record answer" }).click();

  const recorded = page.getByRole("region", { name: "recorded answer" });
  await expect(recorded.getByTestId("recorded-severity")).toContainText(
    `${severityTop} (accepted)`,
  );
  await expect(recorded.getByTestId("recorded-fraud")).toContainText(`${fraudOther} (corrected)`);
  if (!real) await label(page, "STUBBED: worker is a stub; the store and the correction are real");
  await page.screenshot({
    path: `${evidence}/2-correction-captured-${mode}.png`,
    fullPage: true,
  });

  await page.reload();
  await expect(page).toHaveURL(new RegExp(`/decisions/${id}$`));
  const again = page.getByRole("region", { name: "recorded answer" });
  await expect(again.getByTestId("recorded-severity")).toContainText(`${severityTop} (accepted)`);
  await expect(again.getByTestId("recorded-fraud")).toContainText(`${fraudOther} (corrected)`);
  await expect(page.getByRole("button", { name: "Record answer" })).toHaveCount(0);

  // The pair, read from the store rather than the page: the correction beside the prediction
  // it corrects, which is unchanged by it. Then the refusal of a second answer.
  const stored = (await (await request.get(`/api/inferences/decisions/${id}`)).json()) as Decision;
  expect(stored.correction?.prediction_id).toBe(id);
  expect(stored.correction?.answers.fraud).toBe(fraudOther);
  expect(stored.correction?.outcomes).toMatchObject({ severity: "accepted", fraud: "corrected" });
  expect(stored.answers).toEqual(predicted.answers);
  expect(stored.model_revision).toBe(predicted.model_revision);
  const second = await request.post(`/api/inferences/decisions/${id}/correction`, {
    data: { answers: { severity: "low", fraud: fraudTop, risk_score: "1" } },
  });
  expect(second.status()).toBe(409);
  const after = (await (await request.get(`/api/inferences/decisions/${id}`)).json()) as Decision;
  expect(after.correction?.answers.fraud).toBe(fraudOther);

  await page.goto("/decisions");
  const row = page.getByRole("row").filter({ has: page.locator(`a[href="/decisions/${id}"]`) });
  await expect(row).toContainText("recorded");
  if (real) {
    await expectNoStubLabels(page);
    await page.goto("/decide");
    await expectNoStubLabels(page);
  }

  expect(problems, "console errors, including any CSP violation").toEqual([]);
});

test("the eval panel puts the model beside the majority-class baseline", async ({ page }) => {
  await page.goto("/eval");
  await expect(page.getByTestId("calibration")).toContainText("identity transform");
  // Synthetic rows, not a real model's output; the screenshot says so.
  const rows = ["label,p:low,p:high"];
  for (let i = 0; i < 40; i++) {
    const label = i % 4 === 0 ? "high" : "low";
    const pHigh = label === "high" ? (i % 8 === 0 ? 0.8 : 0.35) : i % 5 === 0 ? 0.6 : 0.2;
    rows.push(`${label},${(1 - pHigh).toFixed(2)},${pHigh.toFixed(2)}`);
  }
  await page.getByLabel(/Labelled CSV/).setInputFiles({
    name: "synthetic-alerts.csv",
    mimeType: "text/csv",
    buffer: Buffer.from(rows.join("\n") + "\n"),
  });
  await expect(page.getByTestId("verdict")).toBeVisible();
  await expect(page.getByText("majority baseline (low)")).toBeVisible();
  if (real) await expectNoStubLabels(page);
  await label(page, "SYNTHETIC: a labelled CSV made up for the e2e, not a real model's output");
  await page.screenshot({ path: `${evidence}/3-eval-panel-${mode}.png`, fullPage: true });
});

test("a correction is refused on screen when the prediction is already corrected elsewhere", async ({
  page,
  request,
}) => {
  const made = await request.post("/api/inferences/decisions", {
    data: { text: "A second alert", schema: { name: "alert-triage", version: 1 } },
    timeout: answered.timeout,
  });
  expect(made.status()).toBe(201);
  const { id, answers: stored } = (await made.json()) as Decision;
  const answers = stored ?? [];
  expect(answers.length, "the worker answered").toBeGreaterThan(0);
  await page.goto(`/decisions/${id}`);
  for (const a of answers) {
    await page
      .getByRole("button", { name: `use suggestion (${String(a.options[0]?.option)})` })
      .click();
  }
  const first = await request.post(`/api/inferences/decisions/${id}/correction`, {
    data: {
      answers: Object.fromEntries(answers.map((a) => [a.question, a.options[0]?.option ?? ""])),
    },
  });
  expect(first.status()).toBe(201);
  await page.getByRole("button", { name: "Record answer" }).click();
  await expect(page.getByRole("alert")).toContainText("already corrected");
});

test("a path the dashboard does not have says so, and leads back to Decide", async ({ page }) => {
  await page.goto("/history");
  await expect(page.getByRole("heading", { name: "There is no page at /history" })).toBeVisible();
  await page.getByRole("link", { name: "Back to Decide" }).click();
  await expect(page).toHaveURL(/\/decide$/);
  await expect(page.getByRole("button", { name: "Decide" })).toBeVisible();
});
