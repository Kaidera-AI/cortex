#!/bin/bash
# Install the Cortex v2 client launchers (ship/cli). Contract: contract.md next to this file.
# Refuses before writing anything; never shadows a command it did not install.
set -u

# Versioned v2 names only; live v1 cortex-boot/log/search are never written, linked or shadowed.
COMMANDS="cortex-v2-boot cortex-v2-log cortex-v2-search"
HERE="$(cd "$(dirname "$0")" && pwd)"

refuse() {
    local reason="${1//\\/\\\\}"
    reason="${reason//\"/\\\"}"
    reason="$(printf '%s' "$reason" | tr -d '\000-\037')"
    printf '{"reason": "%s", "unit": "ship/cli"}\n' "$reason" >&2
    exit 2
}

prefix="" client_dir="" core_python="" config="" link_dir="" no_link=0
while [ $# -gt 0 ]; do
    case "$1" in
        --prefix|--client-dir|--core-python|--config|--link-dir)
            [ $# -ge 2 ] || refuse "input_missing:$1"
            case "$1" in
                --prefix) prefix="$2" ;;
                --client-dir) client_dir="$2" ;;
                --core-python) core_python="$2" ;;
                --config) config="$2" ;;
                --link-dir) link_dir="$2" ;;
            esac
            shift 2 ;;
        --no-link) no_link=1; shift ;;
        *) refuse "bad_argument:$1" ;;
    esac
done
[ -n "$prefix" ] || refuse "input_missing:--prefix"
[ -n "$client_dir" ] || refuse "input_missing:--client-dir"
[ -n "$core_python" ] || refuse "input_missing:--core-python"
[ -n "$config" ] || refuse "input_missing:--config"
[ -n "$link_dir" ] || refuse "input_missing:--link-dir"

# Paths are written into the launchers and a sed script: absolute, no shell or sed specials.
for path in "$prefix" "$client_dir" "$core_python" "$config" "$link_dir"; do
    case "$path" in /*) ;; *) refuse "unsafe_path:$path" ;; esac
    case "$path" in *[\"\$\`\\\|\&\']*|*"
"*) refuse "unsafe_path:$path" ;; esac
done

for name in $COMMANDS; do
    [ -f "$client_dir/$name" ] && [ -x "$client_dir/$name" ] || refuse "client_missing:$name"
done
[ -f "$core_python" ] && [ -x "$core_python" ] || refuse "core_python_missing:$core_python"

ours() {
    [ -L "$1" ] && [ "$(readlink "$1")" = "$prefix/bin/$2" ]
}

if [ "$no_link" -eq 0 ]; then
    [ -d "$link_dir" ] || refuse "link_dir_missing:$link_dir"
    for name in $COMMANDS; do
        found="$(command -v "$name" 2>/dev/null || true)"
        if [ -n "$found" ] && ! ours "$found" "$name"; then
            refuse "path_collision:$found"
        fi
        if { [ -e "$link_dir/$name" ] || [ -L "$link_dir/$name" ]; } && ! ours "$link_dir/$name" "$name"; then
            refuse "path_collision:$link_dir/$name"
        fi
    done
fi

mkdir -p "$prefix/bin" "$prefix/cli/agents-empty" || refuse "write_failed:$prefix"
chmod 700 "$prefix/cli/agents-empty"
for name in $COMMANDS; do
    staged="$prefix/bin/.$name.tmp"
    sed -e "s|@CLIENT@|$client_dir/$name|g" \
        -e "s|@CORE_PYTHON@|$core_python|g" \
        -e "s|@CONFIG@|$config|g" \
        -e "s|@EMPTY_AGENTS@|$prefix/cli/agents-empty|g" \
        "$HERE/launcher.py.in" > "$staged" || refuse "write_failed:$staged"
    chmod 755 "$staged"
    mv -f "$staged" "$prefix/bin/$name" || refuse "write_failed:$prefix/bin/$name"
done

if [ "$no_link" -eq 0 ]; then
    for name in $COMMANDS; do
        ln -sfn "$prefix/bin/$name" "$link_dir/$name" || refuse "write_failed:$link_dir/$name"
    done
fi
