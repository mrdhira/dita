#!/bin/sh
# Is every workspace module self-contained without go.work?
#
# go.work resolves a sibling for the compiler, but `go mod tidy`, `go list -m` and
# `go mod verify` read go.mod alone and fail on an unpublished v0.0.0. So a module that
# imports a sibling still needs its own `require` and a local `replace`, and a missing one
# does not break the build -- it breaks the next `go mod tidy`, long after the change.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"

if [ ! -f go.work ]; then
    echo "go-mod-check: no go.work, nothing to check"
    exit 0
fi

MODULE_DIRS=$(go work edit -json | sed -n 's/.*"DiskPath": "\([^"]*\)".*/\1/p' | sed 's|^\./||')

# Anti-vacuity: a go.work that parses to no modules means the parse broke, not that there is
# nothing to check. Without this the gate passes by checking nothing.
if [ -z "$MODULE_DIRS" ]; then
    echo "go-mod-check: go.work lists no modules -- the check would pass without running" >&2
    exit 1
fi

module_path() {
    sed -n 's/^module[ 	][ 	]*//p' "$1/go.mod" | sed 's/"//g' | head -1
}

# Relative path from directory $1 to directory $2, both relative to ROOT. Pure shell:
# `realpath --relative-to` is GNU-only and this runs on macOS too.
relpath() {
    rp_from=$1
    rp_to=$2
    while [ -n "$rp_from" ] && [ -n "$rp_to" ]; do
        case $rp_from in */*) rp_fh=${rp_from%%/*} ;; *) rp_fh=$rp_from ;; esac
        case $rp_to in */*) rp_th=${rp_to%%/*} ;; *) rp_th=$rp_to ;; esac
        [ "$rp_fh" = "$rp_th" ] || break
        case $rp_from in */*) rp_from=${rp_from#*/} ;; *) rp_from= ;; esac
        case $rp_to in */*) rp_to=${rp_to#*/} ;; *) rp_to= ;; esac
    done
    rp_up=""
    rp_rest=$rp_from
    while [ -n "$rp_rest" ]; do
        rp_up="../$rp_up"
        case $rp_rest in */*) rp_rest=${rp_rest#*/} ;; *) rp_rest= ;; esac
    done
    case "$rp_up$rp_to" in
        ../*) printf '%s%s\n' "$rp_up" "$rp_to" ;;
        *)    printf './%s\n' "$rp_to" ;;
    esac
}

# The line a human has to add: every sibling this module names that it has no replace for.
suggest() {
    sg_dir=$1
    for sg_other in $MODULE_DIRS; do
        [ "$sg_other" != "$sg_dir" ] || continue
        [ -f "$sg_other/go.mod" ] || continue
        sg_path=$(module_path "$sg_other")
        [ -n "$sg_path" ] || continue
        if grep -qF "replace $sg_path =>" "$sg_dir/go.mod"; then
            continue
        fi
        # Both matches need a right-hand boundary: one module path is a prefix of another
        # (.../examples/go of .../examples/go2), so a bare substring names the wrong module.
        sg_seen=no
        if grep -qF " $sg_path v" "$sg_dir/go.mod"; then
            sg_seen=yes
        else
            for sg_file in $(find "$sg_dir" -name '*.go' -type f 2>/dev/null); do
                if grep -qF "\"$sg_path\"" "$sg_file" || grep -qF "\"$sg_path/" "$sg_file"; then
                    sg_seen=yes
                    break
                fi
            done
        fi
        if [ "$sg_seen" = yes ]; then
            echo "        add to $sg_dir/go.mod:"
            echo "          require $sg_path v0.0.0"
            echo "          replace $sg_path => $(relpath "$sg_dir" "$sg_other")"
        fi
    done
}

FAILED=0
CHECKED=0

for dir in $MODULE_DIRS; do
    [ -f "$dir/go.mod" ] || continue
    CHECKED=$((CHECKED + 1))

    failing=""
    detail=""
    if ! detail=$(go -C "$dir" mod tidy -diff 2>&1); then
        failing="go mod tidy -diff"
    elif ! detail=$(cd "$dir" && GOWORK=off go build ./... 2>&1); then
        failing="GOWORK=off go build ./..."
    fi

    [ -n "$failing" ] || continue

    FAILED=$((FAILED + 1))
    echo "  FAIL  $dir: $failing"
    printf '%s\n' "$detail" | sed 's/^/        /' | head -6
    suggest "$dir"
done

if [ "$FAILED" -gt 0 ]; then
    echo "go-mod-check: $FAILED module(s) not self-contained without go.work"
    exit 1
fi

echo "go-mod-check: $CHECKED modules tidy and building without go.work"
