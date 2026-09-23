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
