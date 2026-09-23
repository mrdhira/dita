// Fails the build if anything shaped like a credential reached the bundle. The SPA holds no
// secret by design; this is the check that the design held.
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative } from "node:path";

const SHAPES = [
  ["private key block", /-----BEGIN [A-Z ]*PRIVATE KEY-----/],
  ["AWS access key id", /\b(?:AKIA|ASIA)[0-9A-Z]{16}\b/],
  ["GitHub token", /\bgh[pousr]_[A-Za-z0-9]{36,}\b/],
  ["Hugging Face token", /\bhf_[A-Za-z0-9]{30,}\b/],
  ["OpenAI / DeepSeek / Anthropic style key", /\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}\b/],
  ["Slack token", /\bxox[abprs]-[A-Za-z0-9-]{10,}\b/],
  ["Google API key", /\bAIza[0-9A-Za-z_-]{35}\b/],
  ["JSON web token", /\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}/],
  [
    "a secret assigned in the bundle",
    /(?:api[_-]?key|secret|password|token)["']?\s*[:=]\s*["'][^"'\s]{12,}["']/i,
  ],
];

const root = process.argv[2] ?? "dist";
const files = [];
(function walk(dir) {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) walk(path);
    else files.push(path);
  }
})(root);

if (files.length === 0) {
  console.error(`check-dist: ${root} is empty; the check would pass without looking at anything`);
  process.exit(1);
}

const hits = [];
for (const file of files) {
  const text = readFileSync(file, "utf8");
  for (const [shape, pattern] of SHAPES) {
    const match = pattern.exec(text);
    if (match) hits.push(`${relative(root, file)}: ${shape} (${match[0].slice(0, 12)}…)`);
  }
}

if (hits.length > 0) {
  console.error(`check-dist: ${hits.length} key-shaped string(s) in ${root}:`);
  for (const hit of hits) console.error(`  ${hit}`);
  process.exit(1);
}
console.log(`check-dist: ${files.length} files in ${root}, no key shapes`);
