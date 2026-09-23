import { execFileSync } from "node:child_process";

// Playwright stops a webServer by killing its command, and killing `docker run` does not
// reliably stop the container behind it. Without this the next run finds 8443 taken.
export default function teardown() {
  execFileSync("docker", ["rm", "-f", "dita-dashboard-e2e"], { stdio: "ignore" });
}
