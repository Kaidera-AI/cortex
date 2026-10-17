#!/bin/sh
# install.sh (ship/install, nix): verify the signed release BEFORE any effect, then the effect phases in order.
#   install.sh --vault DIR --project KEY [--prefix DIR] [--platform linux-arm64] [--trust-key-file FILE] [--only load]
# Phases wired in this build: check_tools, verify (E0, read-only), load_images (E1). Later phases (make_secret, write_config, provision_roles, bootstrap, serve)
# depend on Bob's render, Cox's custody and Finn's seed: until they are wired the run refuses install_not_wired:<phase> BEFORE any effect, so a half install
# cannot happen. `--only load` is the explicit unit entry (no installed credit). Typed refusals: stderr {"reason":..,"unit":"install"}, exit 2.
set -u
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PINNED_PUBKEY='RWQuhegMfku7e4RltjV64sZmxXHEETzAntDePCQsJPYvXXujVMqKIvHL'   # release key 7BBB4B7E0CE8852E (CTO 6 Oct); --trust-key-file is explicit and printed
PODMAN_FLOOR='6.0.2'
UNWIRED='make_secret write_config provision_roles bootstrap serve'
refuse() { printf '{"reason":"%s","unit":"install"}\n' "$1" >&2; exit 2; }
release_dir=$HERE vault=""; project=""; prefix="${HOME:-/tmp}/.local/share/kaidera/cortex-v2"; keyfile=""; platform=""; only=""
while [ $# -gt 0 ]; do
  case $1 in
    --release-dir) release_dir=$2; shift 2 ;;
    --vault) vault=$2; shift 2 ;;
    --project) project=$2; shift 2 ;;
    --prefix) prefix=$2; shift 2 ;;
    --trust-key-file) keyfile=$2; shift 2 ;;
    --platform) platform=$2; shift 2 ;;
    --only) only=$2; shift 2 ;;
    *) refuse "bad_argument:$1" ;;
  esac
done
[ -n "$vault" ] && [ -n "$project" ] || refuse missing_argument:vault_or_project
if [ -z "$platform" ]; then
  case $(uname -m) in arm64|aarch64) platform=linux-arm64 ;; x86_64|amd64) platform=linux-amd64 ;; *) refuse "platform_not_in_release:$(uname -m)" ;; esac
fi

check_tools() {  # by effect, never by exit code alone: a tool that cannot report its version is missing
  for t in podman python3; do command -v "$t" >/dev/null 2>&1 || refuse "missing_tool:$t"; done
  found=$(podman --version 2>/dev/null | awk '{print $NF}') ; [ -n "$found" ] || refuse missing_tool:podman
  IFS=. read -r a b c <<EOF2
$found
EOF2
  c=${c%%[!0-9]*}; a=${a:-0}; b=${b:-0}; c=${c:-0}
  if [ "$a" -lt 6 ] || { [ "$a" -eq 6 ] && [ "$b" -eq 0 ] && [ "$c" -lt 2 ]; }; then refuse "podman_too_old:$found"; fi
}

record_object() {  # append-or-replace one object of OURS in the install record (python3 only writes JSON)
  mkdir -p "$prefix" && chmod 700 "$prefix"
  python3 -I - "$prefix/install-record.json" "$release_id" "$1" "$2" "$3" <<'PY'
import json, os, sys
path, release, kind, name, ident = sys.argv[1:6]
doc = json.load(open(path)) if os.path.exists(path) else {'release_id': release, 'objects': []}
doc['release_id'] = release
doc['objects'] = [o for o in doc['objects'] if o['name'] != name] + [{'kind': kind, 'name': name, 'id': ident}]
doc['objects'].sort(key=lambda o: o['name'])
open(path + '.tmp', 'w').write(json.dumps(doc, sort_keys=True, indent=1) + '\n')
os.replace(path + '.tmp', path)
PY
}

load_images() {  # E1: this platform's two archives only, tagged by id, recorded the moment they exist
  release_id=$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["release_id"])' "$release_dir/release.json") || refuse release_json_invalid
  for role in core api; do
    out=$(podman load -q -i "$release_dir/cortex-$role-$platform.oci.tar") || refuse "load_failed:$role"
    id=${out##*: }; id=$(printf '%s' "$id" | tail -n 1)
    case $id in sha256:*) ;; *) refuse "load_unrecognised:$role" ;; esac
    name="localhost/cortex-v2-$role:$release_id"
    podman tag "$id" "$name" || refuse "tag_failed:$role"
    record_object image "$name" "$id"
  done
}

check_tools
VERIFY_SOURCED=1 . "$release_dir/verify.sh"
if [ -n "$keyfile" ]; then printf 'trust key file: %s\n' "$keyfile" >&2; verify_release "$release_dir" --trust-key-file "$keyfile" --platform "$platform" || exit 2
else verify_release "$release_dir" --platform "$platform" || exit 2; fi
if [ "$only" = load ]; then load_images; exit 0; fi
[ -z "$only" ] || refuse "bad_argument:--only $only"
for phase in $UNWIRED; do refuse "install_not_wired:$phase"; done
