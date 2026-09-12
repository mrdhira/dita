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

# Services declare requires-python >=3.14; the patch in .tool-versions is for local tooling,
# so the minor is what has to match.
PY_WANT=$(pinned python)
PY_HAVE=$(command -v python3 >/dev/null 2>&1 && python3 --version 2>/dev/null | awk '{print $2}')
case "$PY_HAVE" in 3.14.*) m=yes ;; *) m=no ;; esac
check python python "${PY_WANT%.*}.x" "$PY_HAVE" "$m"

GO_WANT=$(pinned golang)
GO_HAVE=$(command -v go >/dev/null 2>&1 && go version 2>/dev/null | awk '{print $3}' | sed 's/^go//')
[ "$GO_HAVE" = "$GO_WANT" ] && m=yes || m=no
check go golang "$GO_WANT" "$GO_HAVE" "$m"

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
