import { cleanup, configure } from "@testing-library/react";
import { afterEach, vi } from "vitest";

// Headroom, not a threshold: a cold first render on a busy machine took 1.4-1.5 s (default 1 s).
configure({ asyncUtilTimeout: 5_000 });

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});
