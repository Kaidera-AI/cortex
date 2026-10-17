#!/bin/sh
# verify_release DIR [--trust-key-file FILE] [--platform linux-arm64]   (ship/sign, nix). Read-only: no podman, nothing written.
# 1 minisign signature of SHA256SUMS, 2 every file THIS platform needs matches SHA256SUMS (release.json included), before any effect.
# Typed refusal on stderr as {"reason":..,"unit":"verify"}, exit 2. Key: --trust-key-file, else the pinned $PINNED_PUBKEY of install.sh.
# Sourced by install.sh with VERIFY_SOURCED=1 (functions only); run directly it verifies its arguments.
verify_refuse() { printf '{"reason":"%s","unit":"verify"}\n' "$1" >&2; return 2; }
sha_of() { if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi; }
verify_release() {
  dir=$1; shift; keyfile=""; platform=""
  while [ $# -gt 0 ]; do
    case $1 in
      --trust-key-file) keyfile=$2; shift 2 ;;
      --platform) platform=$2; shift 2 ;;
      *) verify_refuse "bad_argument:$1"; return 2 ;;
    esac
  done
  command -v minisign >/dev/null 2>&1 || { verify_refuse missing_tool:minisign; return 2; }
  command -v sha256sum >/dev/null 2>&1 || command -v shasum >/dev/null 2>&1 || { verify_refuse missing_tool:sha256sum; return 2; }
  [ -f "$dir/SHA256SUMS" ] && [ -f "$dir/SHA256SUMS.minisig" ] || { verify_refuse release_signature_missing; return 2; }
  if [ -n "$keyfile" ]; then
    minisign -V -q -p "$keyfile" -m "$dir/SHA256SUMS" >/dev/null 2>&1 || { verify_refuse release_signature_invalid; return 2; }
  else
    minisign -V -q -P "${PINNED_PUBKEY:-}" -m "$dir/SHA256SUMS" >/dev/null 2>&1 || { verify_refuse release_signature_invalid; return 2; }
  fi
  grep -q "  cortex-[a-z]*-$platform\.oci\.tar\$" "$dir/SHA256SUMS" || { verify_refuse platform_not_in_release; return 2; }
  while IFS= read -r line; do
    want=${line%%  *}; name=${line#*  }
    case $name in
      *linux-arm64*|*linux-amd64*) case $name in *"$platform"*) ;; *) continue ;; esac ;;
    esac
    [ -f "$dir/$name" ] || { verify_refuse "artifact_missing:$name"; return 2; }
    [ "$(sha_of "$dir/$name")" = "$want" ] || { verify_refuse "artifact_digest_mismatch:$name"; return 2; }
  done < "$dir/SHA256SUMS"
  return 0
}
[ -n "${VERIFY_SOURCED:-}" ] || { verify_release "$@"; exit $?; }
