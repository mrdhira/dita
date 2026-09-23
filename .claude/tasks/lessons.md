# Lessons

## Never commit on `main`
- **Pattern:** a runner prompt said "on the current branch" while the order named a branch and a
  PR; I committed five commits onto `main`.
- **Rule:** in this repo `main` is never committed to directly. "The current branch" is not
  permission to commit to `main`. Before the first commit, if `git branch --show-current` is
  `main`, create the order's branch (or `feat/<unit>`) first.
- **Recovery that loses nothing:** `git checkout -b <branch>` at HEAD, then
  `git branch -f main origin/main`.
