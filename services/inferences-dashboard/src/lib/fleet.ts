import type { WorkerReport } from "../api/client";

export type TryRoute = "/embed" | "/rerank" | "/decide";

export interface DeployedService {
  id: string;
  container: string;
  role: string;
  tryRoute: TryRoute;
  blastRadius: string;
}

export interface UndeployedService {
  id: string;
  container: string;
  role: string;
  reason: string;
}

export type Service = DeployedService | UndeployedService;

export const isDeployed = (s: Service): s is DeployedService => "tryRoute" in s;

const HINDSIGHT = "Hindsight recall and consolidation use this: while it is not ready, both fail.";

/**
 * The intended fleet. Frontend knowledge, not API data: the gateway reports only the three
 * services it is configured for, and the other three have no HTTP surface to report from.
 */
export const FLEET: readonly Service[] = [
  {
    id: "embedding",
    container: "inferences-embedding",
    role: "text embeddings",
    tryRoute: "/embed",
    blastRadius: HINDSIGHT,
  },
  {
    id: "reranker",
    container: "inferences-reranker",
    role: "reranking",
    tryRoute: "/rerank",
    blastRadius: HINDSIGHT,
  },
  {
    id: "system-one",
    container: "inferences-system-one",
    role: "decisions",
    tryRoute: "/decide",
    blastRadius:
      "Serves /decide: the Decide page and every recorded prediction fail while it is not ready.",
  },
  { id: "ocr", container: "inferences-ocr", role: "OCR", reason: "no HTTP surface: DIP only" },
  { id: "stt", container: "inferences-stt", role: "speech to text", reason: "no code" },
  { id: "tts", container: "inferences-tts", role: "text to speech", reason: "no code" },
];

export const serviceById = (id: string | undefined) => FLEET.find((s) => s.id === id);

export type Tone = "good" | "warn" | "bad" | "idle";

export interface StateView {
  word: string;
  tone: Tone;
  shape: string;
  sentence: string;
}

const fixed: Record<string, Omit<StateView, "sentence"> & { sentence?: string }> = {
  ready: { word: "ready", tone: "good", shape: "●" },
  busy: {
    word: "busy",
    tone: "warn",
    shape: "◐",
    sentence: "reachable, at its connection cap · requests are refused, not queued",
  },
  no_model: {
    word: "no model",
    tone: "warn",
    shape: "○",
    sentence: "alive, not serving · frees RAM · requests fail until a model is loaded",
  },
  not_running: {
    word: "stopped",
    tone: "bad",
    shape: "■",
    sentence: "container not running — a host action, not something this console can change",
  },
  unhealthy: { word: "unhealthy", tone: "bad", shape: "▲" },
  unreachable: { word: "unreachable", tone: "bad", shape: "◆" },
  timeout: { word: "timeout", tone: "bad", shape: "◆" },
  client_gone: { word: "degraded", tone: "warn", shape: "◇" },
};

/** Design §6: every state is a word, a colour and a shape, with the sentence it means. */
export function describeState(report: WorkerReport): StateView {
  const known = fixed[report.state];
  const modelId = typeof report.info?.model_id === "string" ? report.info.model_id : null;
  const reported =
    report.error ??
    (report.health_status !== null ? `its own /health answered ${report.health_status}` : "");
  if (!known) return { word: report.state, tone: "idle", shape: "?", sentence: reported };
  if (known.sentence) return { ...known, sentence: known.sentence };
  if (report.state === "ready") {
    return { ...known, sentence: modelId ? `serving ${modelId}` : "serving" };
  }
  return { ...known, sentence: reported };
}

export function notDeployed(service: UndeployedService): StateView {
  return { word: "not deployed", tone: "idle", shape: "–", sentence: service.reason };
}
