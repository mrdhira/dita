#!/bin/sh
# Is this machine ready to work in this repo? Linux and macOS.
# Expected versions come from .tool-versions so there is one source of truth.
set -u

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
OS=$(uname -s)
FAILURES=0

pinned() { awk -v tool="$1" '$1 == tool { print $2 }' "$ROOT/.tool-versions"; }

pass() { printf '  PASS  %-8s %s\n' "$1" "$2"; }
fail() {
    printf '  FAIL  %-8s %s\n' "$1" "$2"
    printf '        fix: %s\n' "$3"
    FAILURES=$((FAILURES + 1))
}

# asdf is the cross-platform path, and the one .tool-versions describes. Homebrew is the
# macOS shortcut. The asdf plugin name is the .tool-versions key, which is not always the
# command name: `go` comes from the `golang` plugin.
hint() {
    plugin=$1
    version=$2
    case "$OS:$plugin" in
        Darwin:docker) echo "brew install --cask docker, then start Docker Desktop" ;;
        Linux:docker)  echo "see https://docs.docker.com/engine/install/ , then: sudo systemctl start docker" ;;
        Darwin:git)    echo "xcode-select --install    (or: brew install git)" ;;
        Linux:git)     echo "sudo apt-get install git" ;;
        Darwin:*)      echo "asdf plugin add $plugin && asdf install $plugin $version && asdf set $plugin $version    (or: brew install $plugin)" ;;
        *)             echo "asdf plugin add $plugin && asdf install $plugin $version && asdf set $plugin $version" ;;
    esac
}

check() {  # check <tool> <asdf-plugin> <expected> <actual-or-empty> <match>
    tool=$1; plugin=$2; expected=$3; actual=$4; match=$5
    if [ -z "$actual" ]; then
        fail "$tool" "not found on PATH" "$(hint "$plugin" "$expected")"
    elif [ "$match" = "yes" ]; then
        pass "$tool" "$actual"
    else
        fail "$tool" "found $actual, expected $expected" "$(hint "$plugin" "$expected")"
    fi
}

echo "dita doctor  ($OS)"
echo

UV_WANT=$(pinned uv)
UV_HAVE=$(command -v uv >/dev/null 2>&1 && uv --version 2>/dev/null | awk '{print $2}')
[ "$UV_HAVE" = "$UV_WANT" ] && m=yes || m=no
check uv uv "$UV_WANT" "$UV_HAVE" "$m"

# The interpreter that runs this repo is the one uv provisions for the workspace, not
# whatever `python3` is on PATH -- that one is never used here. Exact patch, matching
# .python-version and the runtime image tag.
PY_WANT=$(cat "$ROOT/.python-version" 2>/dev/null)
if [ -z "$UV_HAVE" ]; then
    fail python "skipped, uv provisions it and uv is missing" "install uv first"
else
    # Use the existing workspace venv when there is one; otherwise ask uv what it would
    # resolve, so a cold machine is not told to install 300 MB of packages to answer.
    if [ -d "$ROOT/.venv" ]; then
        PY_HAVE=$(cd "$ROOT" && uv run --frozen --no-sync python -V 2>/dev/null | awk '{print $2}')
        PY_VIA="workspace venv"
    else
        PY_PATH=$(cd "$ROOT" && uv python find "$PY_WANT" 2>/dev/null)
        PY_HAVE=""
        [ -x "$PY_PATH" ] && PY_HAVE=$("$PY_PATH" -V 2>/dev/null | awk '{print $2}')
        PY_VIA="uv, no venv yet"
    fi
    if [ "$PY_HAVE" = "$PY_WANT" ]; then
        pass python "$PY_HAVE ($PY_VIA)"
    elif [ -z "$PY_HAVE" ]; then
        fail python "uv has no $PY_WANT for this workspace" "uv python install $PY_WANT"
    else
        fail python "uv resolves $PY_HAVE, expected exactly $PY_WANT" "uv python install $PY_WANT"
    fi
fi

GO_WANT=$(pinned golang)
GO_HAVE=$(command -v go >/dev/null 2>&1 && go version 2>/dev/null | awk '{print $3}' | sed 's/^go//')
[ "$GO_HAVE" = "$GO_WANT" ] && m=yes || m=no
check go golang "$GO_WANT" "$GO_HAVE" "$m"

# The compiler on PATH is not what builds this repo; the directives are. A `go` line at the
# full patch is the pin for a module -- a `toolchain` line equal to it makes the module
# untidy and `go build` refuses it. go.work is the one file that takes both.
#
# Modules are found on disk, not through git: an untracked go.mod still gets built, and
# outside a checkout `git ls-files` returns nothing, which used to read as "no go modules
# yet" on a tree with three of them.
GO_MODS=$(find "$ROOT" -name go.mod -not -path '*/.venv/*' -not -path '*/node_modules/*' \
    -not -path '*/.git/*' 2>/dev/null | sort)
GO_WORK="$ROOT/go.work"

if [ -z "$GO_MODS" ]; then
    pass toolchain "no go modules on disk"
else
    drift=""
    redundant=""
    for mod in $GO_MODS; do
        grep -qx "go $GO_WANT" "$mod" || drift="$drift ${mod#"$ROOT/"}"
        # The failure the comment above exists to prevent: a toolchain line equal to the
        # go line. `go build` refuses it and `go mod tidy` deletes it.
        grep -qx "toolchain go$GO_WANT" "$mod" && redundant="$redundant ${mod#"$ROOT/"}"
    done

    if [ -n "$drift" ]; then
        fail toolchain "go.mod not pinned to $GO_WANT:$drift" "set 'go $GO_WANT' in each"
    elif [ -n "$redundant" ]; then
        fail toolchain "redundant 'toolchain go$GO_WANT' in:$redundant" \
            "remove it; go build refuses a toolchain line equal to the go line"
    elif [ ! -f "$GO_WORK" ]; then
        fail toolchain "no go.work beside $(basename "$ROOT")" "create it with 'go work init'"
    elif ! grep -qx "go $GO_WANT" "$GO_WORK"; then
        fail toolchain "go.work has no 'go $GO_WANT'" "set it in go.work"
    elif ! grep -qx "toolchain go$GO_WANT" "$GO_WORK"; then
        fail toolchain "go.work has no 'toolchain go$GO_WANT'" "add it to go.work"
    else
        pass toolchain "$(printf '%s\n' "$GO_MODS" | wc -l | tr -d ' ') go.mod pinned to $GO_WANT, go.work carries both"
    fi
fi

# go.work resolves a sibling module for the compiler but not for `go mod tidy`, so a missing
# local `replace` is invisible until someone tidies. Checked here, before they do.
if [ ! -f "$GO_WORK" ]; then
    :
elif MODCHECK=$("$ROOT/scripts/go-mod-check.sh" 2>&1); then
    pass modules "$(printf '%s\n' "$MODCHECK" | sed -n '$s/^go-mod-check: //p')"
else
    fail modules "a workspace module does not stand up without go.work" \
        "add the lines below to that module's go.mod, then: make go-mod-check"
    printf '%s\n' "$MODCHECK" | sed 's/^/        /'
fi

# The dashboard is the one JS unit: node runs its tooling and pnpm owns its lockfile.
NODE_WANT=$(pinned nodejs)
NODE_HAVE=$(command -v node >/dev/null 2>&1 && node --version 2>/dev/null | sed 's/^v//')
[ "$NODE_HAVE" = "$NODE_WANT" ] && m=yes || m=no
check node nodejs "$NODE_WANT" "$NODE_HAVE" "$m"

PNPM_WANT=$(pinned pnpm)
PNPM_HAVE=$(command -v pnpm >/dev/null 2>&1 && pnpm --version 2>/dev/null)
[ "$PNPM_HAVE" = "$PNPM_WANT" ] && m=yes || m=no
check pnpm pnpm "$PNPM_WANT" "$PNPM_HAVE" "$m"

GIT_HAVE=$(command -v git >/dev/null 2>&1 && git --version 2>/dev/null | awk '{print $3}')
check git git "any" "$GIT_HAVE" yes

if ! command -v docker >/dev/null 2>&1; then
    fail docker "not found on PATH" "$(hint docker any)"
elif docker info >/dev/null 2>&1; then
    pass docker "$(docker --version | awk '{print $3}' | tr -d ,) (daemon reachable)"
else
    fail docker "installed but the daemon is not reachable" "$(hint docker any)"
fi

echo
if [ -z "$UV_HAVE" ]; then
    fail lock "skipped, uv is missing" "install uv first"
elif (cd "$ROOT" && uv lock --check >/dev/null 2>&1); then
    pass lock "uv.lock is in sync with the manifests"
else
    fail lock "uv.lock is stale or the workspace does not resolve" "make lock"
fi

echo
if [ "$FAILURES" -eq 0 ]; then
    echo "ready."
else
    echo "$FAILURES check(s) failed."
fi
exit $([ "$FAILURES" -eq 0 ] && echo 0 || echo 1)
