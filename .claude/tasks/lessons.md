# Lessons
## Never commit on `main`
- **Pattern:** a runner prompt said "on the current branch" while the order named a branch and a
  PR; I committed five commits onto `main`.
- **Rule:** in this repo `main` is never committed to directly. "The current branch" is not
  permission to commit to `main`. Before the first commit, if `git branch --show-current` is
  `main`, create the order's branch (or `feat/<unit>`) first.
- **Recovery that loses nothing:** `git checkout -b <branch>` at HEAD, then
  `git branch -f main origin/main`.

## Never `pkill -f` a pattern the command itself contains
- **Pattern:** twice, `pkill -f "<text>"` matched its own shell's command line and killed the
  command that was cleaning up (exit 144), so the fix after it never ran.
- **Rule:** kill by PID (`pgrep -f ... | grep -v pgrep`, then `kill <pid>`), or by container name.

## `eslint --fix` can remove a cast another tsconfig needs
- **Pattern:** a type-aware fix removed an assertion ESLint judged unnecessary under one project;
  the test's own tsconfig needed it and `tsc -b` then failed.
- **Rule:** after any `--fix`, run `tsc -b` before believing the tree is green; prefer a typed query
  (`getByRole<HTMLTextAreaElement>`) over a cast.

## Refuse-rather-than-repair is a per-file decision, not a store-wide one
- **Pattern:** I made replay refuse any unreadable prediction or correction, as briefed. One damaged
  byte then crash-looped the gateway (`restart: unless-stopped`), taking embed, rerank and decide down.
- **Rule:** before making a read path fatal, ask what the process does next. Fatal is right for the
  small files that define the rules (templates); high-volume records are quarantined to a sidecar,
  counted on `/stats`, and skipped. Weigh availability against correctness per file, and write the
  reason in the doc comment.

## A mutation run must restore the tree even when it is killed
- **Pattern:** I wrapped a mutation script in `timeout`; it was killed mid-mutant, the `finally` never
  ran, and a mutant stayed in `handler.go` while the user was verifying the tree.
- **Rule:** never put `timeout` around a script that edits files. Back up each file before mutating,
  and after any interrupted run, grep for the mutant text before reporting the tree as clean. A mutant
  that makes a test block, not fail, needs a per-test `-timeout`, not a kill from outside.

## A mutant that removes a network seam reaches the network
- **Pattern:** mutating chat to ignore its `baseURL` seam sent the test's happy path to the real
  `api.deepseek.com` (with a fake key). The committed suite was offline; the mutation run was not.
- **Rule:** before mutating a seam that points at an outside service, make that service
  unreachable for the run (an unroutable proxy in the environment, e.g. `HTTPS_PROXY=http://127.0.0.1:1`),
  or skip that mutant and say so.

## A client that decides a server verdict becomes a second source of truth
- **Pattern:** I made Decide judge "usable" with the editor's authoring rules (`draftSchema`), while
  the runtime deliberately keeps older templates running; the live template would have been blocked.
- **Rule:** when a verdict has an owner (the runtime, the server), the client displays it. Its own
  rules validate only its own state; any fallback for an older server says so in a comment.
- **Check:** before reusing a validation schema for a different question ("may this run?" versus
  "may this be saved?"), find who answers that question on the server.

## Name a metric by what its HELP line says it counts
- **Pattern:** I labelled `dita_worker_ops_total` "requests handled"; on the live worker it was
  3,727 `readyz` probes and no inference at all, because HTTP inferences never touch the DIP op
  counter. The unit tests passed: the fixture was real, the label was the invention.
- **Rule:** before titling a series, read its `# HELP` and one live scrape, and check which path
  increments it. Where two paths exist (DIP and HTTP), say which one the number covers.

## Absence of data is not a state
- **Pattern:** the console printed "No model is resident … requests fail" whenever `/info` was
  null, which is also what a pending query, a failed gateway, a `busy` worker and a failed second
  probe look like. A `ready · serving` row said "none resident" beside itself.
- **Rule:** before rendering a claim from a null, list every path that produces that null. Claim
  the state only from the field that reports it (`state === "no_model"`); every other null is
  "unknown", said as such.
- **Check:** grep the UI for negative claims ("no", "none", "not") and find each one's source field.
