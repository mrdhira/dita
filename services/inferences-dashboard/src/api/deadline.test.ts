// @vitest-environment node
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { GATEWAY_PROBE_TIMEOUT_MS, READ_DEADLINE_MS } from "./client";

const config = readFileSync(
  new URL("../../../dita-orchestrator/handler/inferences/config.go", import.meta.url),
  "utf8",
);

describe("the read deadline and the gateway's probe timeout", () => {
  it("uses the gateway's own probe timeout", () => {
    const seconds = /^const DefaultProbeTimeout = (\d+) \* time\.Second$/m.exec(config)?.[1];
    expect(seconds).toBeDefined();
    expect(Number(seconds) * 1000).toBe(GATEWAY_PROBE_TIMEOUT_MS);
  });

  it("outlasts the gateway's worst case for /workers, two probes in a row, by a margin", () => {
    const worstCase = 2 * GATEWAY_PROBE_TIMEOUT_MS;
    expect(READ_DEADLINE_MS - worstCase).toBeGreaterThanOrEqual(3_000);
  });
});
