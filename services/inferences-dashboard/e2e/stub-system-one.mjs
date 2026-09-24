// A STUB of inferences-system-one, not a model. The real worker is not merged, so this
// answers POST /decide in the documented stub contract (the one shape the orchestrator's
// decisions/worker.go adapter reads) with probabilities derived from a hash of the text.
// It labels itself everywhere a person could see: model_id and model_revision say STUB.
import { createHash } from "node:crypto";
import { createServer } from "node:http";

const port = Number(process.env.STUB_PORT ?? 18801);
const noModel = process.env.STUB_NO_MODEL === "1";

function answer(text, question) {
  const weights = question.options.map((option) => {
    const h = createHash("sha256").update(`${text}\u0000${question.name}\u0000${option}`).digest();
    return 1 + h[0];
  });
  const total = weights.reduce((a, b) => a + b, 0);
  const probabilities = {};
  question.options.forEach((option, i) => {
    probabilities[option] = Number((weights[i] / total).toFixed(4));
  });
  const confidence = Math.max(...Object.values(probabilities));
  return { name: question.name, probabilities, confidence };
}

function send(res, status, body) {
  res.writeHead(status, { "Content-Type": "application/json" });
  res.end(JSON.stringify(body));
}

createServer((req, res) => {
  if (req.method === "GET" && req.url === "/health") return send(res, noModel ? 503 : 200, {});
  if (req.method === "GET" && req.url === "/info") {
    return send(res, 200, {
      model_id: "stub-system-one",
      model_revision: "STUB-not-a-model",
      stub: true,
    });
  }
  if (req.method !== "POST" || req.url !== "/decide") return send(res, 404, { error: "not found" });
  let raw = "";
  req.on("data", (chunk) => (raw += chunk));
  req.on("end", () => {
    if (noModel) {
      return send(res, 503, {
        error: "no model is loaded; the orchestrator has not loaded one yet",
        error_type: "Unhealthy",
      });
    }
    const { text, questions } = JSON.parse(raw);
    send(res, 200, {
      model_id: "stub-system-one",
      model_revision: "STUB-not-a-model",
      answers: questions.map((q) => answer(text, q)),
    });
  });
}).listen(port, "127.0.0.1", () => {
  console.log(`stub-system-one (a STUB, not a model) on http://127.0.0.1:${port}`);
});
