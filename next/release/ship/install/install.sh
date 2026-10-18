#!/bin/sh
# install.sh (ship/install, nix): verify the signed release BEFORE any effect, then the effect phases in order.
#   install.sh --vault DIR --project KEY [--prefix DIR] [--platform linux-arm64] [--trust-key-file FILE] [--port N] [--only load|write_config]
# Phases wired in this build: check_tools, verify (E0, read-only), load_images (E1), write_config (unit entry `--only write_config`, S2). Later phases (make_secret, provision_roles, bootstrap, serve)
# depend on Bob's render, Cox's custody and Finn's seed: until they are wired the run refuses install_not_wired:<phase> BEFORE any effect, so a half install
# cannot happen. `--only load` and `--only write_config` are the explicit unit entries (no installed credit). Typed refusals: stderr {"reason":..,"unit":"install"}, exit 2.
set -u
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PINNED_PUBKEY='RWQuhegMfku7e4RltjV64sZmxXHEETzAntDePCQsJPYvXXujVMqKIvHL'   # release key 7BBB4B7E0CE8852E (CTO 6 Oct); --trust-key-file is explicit and printed
PODMAN_FLOOR='6.0.2'
# Full order: make_secret, write_config, provision_roles, bootstrap, serve. make_secret stays first and unwired, so a full run refuses before ANY effect (no half install);
# write_config runs only through its unit entry until make_secret is wired.
UNWIRED='make_secret provision_roles bootstrap serve'
# The verifier lives HERE, in the trusted bootstrap, and is the ONLY verification authority (HALE-A2-NIX-SIGN-001): nothing selected from the release folder is
# sourced or executed before the signature of SHA256SUMS is authenticated. Read-only: no podman, nothing written. verify_release DIR [--trust-key-file F] [--platform P].
# 1 minisign signature of SHA256SUMS (key: --trust-key-file, else $PINNED_PUBKEY), 2 every file THIS platform needs matches SHA256SUMS (release.json included).
verify_refuse() { printf '{"reason":"%s","unit":"verify"}\n' "$1" >&2; return 2; }
sha_of() { if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi; }
verify_release() {
  v_dir=$1; shift; v_key=""; v_plat=""
  while [ $# -gt 0 ]; do
    case $1 in
      --trust-key-file) v_key=$2; shift 2 ;;
      --platform) v_plat=$2; shift 2 ;;
      *) verify_refuse "bad_argument:$1"; return 2 ;;
    esac
  done
  command -v minisign >/dev/null 2>&1 || { verify_refuse missing_tool:minisign; return 2; }
  command -v sha256sum >/dev/null 2>&1 || command -v shasum >/dev/null 2>&1 || { verify_refuse missing_tool:sha256sum; return 2; }
  [ -f "$v_dir/SHA256SUMS" ] && [ -f "$v_dir/SHA256SUMS.minisig" ] || { verify_refuse release_signature_missing; return 2; }
  if [ -n "$v_key" ]; then
    minisign -V -q -p "$v_key" -m "$v_dir/SHA256SUMS" >/dev/null 2>&1 || { verify_refuse release_signature_invalid; return 2; }
  else
    minisign -V -q -P "${PINNED_PUBKEY:-}" -m "$v_dir/SHA256SUMS" >/dev/null 2>&1 || { verify_refuse release_signature_invalid; return 2; }
  fi
  grep -q "  cortex-[a-z]*-$v_plat\.oci\.tar\$" "$v_dir/SHA256SUMS" || { verify_refuse platform_not_in_release; return 2; }
  while IFS= read -r line; do
    want=${line%%  *}; name=${line#*  }
    case $name in
      *linux-arm64*|*linux-amd64*) case $name in *"$v_plat"*) ;; *) continue ;; esac ;;
    esac
    [ -f "$v_dir/$name" ] || { verify_refuse "artifact_missing:$name"; return 2; }
    [ "$(sha_of "$v_dir/$name")" = "$want" ] || { verify_refuse "artifact_digest_mismatch:$name"; return 2; }
  done < "$v_dir/SHA256SUMS"
  return 0
}
refuse() { printf '{"reason":"%s","unit":"install"}\n' "$1" >&2; exit 2; }
verify_only=""; release_dir=$HERE vault=""; project=""; prefix="${HOME:-/tmp}/.local/share/kaidera/cortex-v2"; keyfile=""; platform=""; only=""; port=""
while [ $# -gt 0 ]; do
  case $1 in
    --release-dir) release_dir=$2; shift 2 ;;
    --vault) vault=$2; shift 2 ;;
    --project) project=$2; shift 2 ;;
    --prefix) prefix=$2; shift 2 ;;
    --trust-key-file) keyfile=$2; shift 2 ;;
    --platform) platform=$2; shift 2 ;;
    --only) only=$2; shift 2 ;;
    --port) port=$2; shift 2 ;;
    --verify-only) verify_only=1; shift ;;
    *) refuse "bad_argument:$1" ;;
  esac
done
if [ -z "$platform" ]; then
  case $(uname -m) in arm64|aarch64) platform=linux-arm64 ;; x86_64|amd64) platform=linux-amd64 ;; *) refuse "platform_not_in_release:$(uname -m)" ;; esac
fi
if [ -n "$verify_only" ]; then   # explicit read-only entry (operators, tests): the same verifier, nothing else
  if [ -n "$keyfile" ]; then verify_release "$release_dir" --trust-key-file "$keyfile" --platform "$platform"; else verify_release "$release_dir" --platform "$platform"; fi
  exit $?
fi
[ -n "$vault" ] && [ -n "$project" ] || refuse missing_argument:vault_or_project

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

write_config() {  # S2: the ONE installer writer of <vault>/<project>/config/cortex.json. The helper beside this script must BE a signed member (its bytes equal a line of the
  # authenticated SHA256SUMS) and runs only with the Core source two levels above this script's tree (source layout next/release/ship/install -> next/src); no release-selected code.
  case $port in ''|*[!0-9]*) refuse missing_argument:port ;; esac
  helper="$HERE/config_writer.py"; core_src="$HERE/../../../src"
  line=$(grep "  config_writer\.py\$" "$release_dir/SHA256SUMS") || refuse artifact_missing:config_writer.py
  [ -f "$helper" ] && [ "$(sha_of "$helper")" = "${line%%  *}" ] || refuse artifact_digest_mismatch:config_writer.py
  [ -d "$core_src/cortex_core" ] || refuse write_config_core_missing
  core_src=$(CDPATH= cd -- "$core_src" && pwd)
  python3 -I -B "$helper" --core-src "$core_src" --vault "$vault" --project "$project" --port "$port" >/dev/null || exit 2
}

check_tools
if [ -n "$keyfile" ]; then printf 'trust key file: %s\n' "$keyfile" >&2; verify_release "$release_dir" --trust-key-file "$keyfile" --platform "$platform" || exit 2
else verify_release "$release_dir" --platform "$platform" || exit 2; fi
if [ "$only" = load ]; then load_images; exit 0; fi
if [ "$only" = write_config ]; then write_config; exit 0; fi
[ -z "$only" ] || refuse "bad_argument:--only $only"
for phase in $UNWIRED; do refuse "install_not_wired:$phase"; done
