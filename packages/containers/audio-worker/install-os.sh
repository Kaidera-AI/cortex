#!/bin/sh
# Build-only apt transaction. Signed immutable indices also pin all transitive .debs.
set -eu
rm -f /etc/apt/sources.list /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources
cp /build/debian-snapshot.sources /etc/apt/sources.list.d/debian.sources
apt-get update
for receipt in \
  trixie:98b25b5cd185c59d34aa6e4c3e9b5b8f01bbe9d104fe2dcfbcd30dc0a14a59ed \
  trixie-security:19a20e3818e39ae271b114aa048a20d961b3f4604c013bed46c2e6c991d08dbb \
  trixie-updates:f02d96f99db560df6328eeb584f034ca1d9fc4f25eb644144b042a0193165def; do
  suite="${receipt%%:*}"; digest="${receipt#*:}"
  set -- /var/lib/apt/lists/*_dists_${suite}_InRelease
  [ "$#" -eq 1 ]
  printf '%s  %s\n' "$digest" "$1" | sha256sum -c -
done
packages='curl=8.14.1-2+deb13u4 ffmpeg=7:7.1.5-0+deb13u1'
# Word splitting is intentional: this fixed reviewed list contains no user input.
apt-get install -y --download-only --reinstall --no-install-recommends $packages
architecture="$(dpkg --print-architecture)"
case "$architecture" in
  amd64) receipts='curl:7bd23df36e65ca32a5bbd81f67ca311b9e0c122212c70d0733f4e45d745789d2 ffmpeg:8669bc77e55393c14e0dd257ba9a916007cb6da0e1074c9b5e1d53812024f2af' ;;
  arm64) receipts='curl:9881fbb76558c576488c90e0a1cb1cc2d2154b1e39a70dd44e2b736200146afe ffmpeg:6ec9c42306df30803c731c9e53300627257fd59d2f5a80b394c14cec5fd6d5ef' ;;
  *) exit 1 ;;
esac
for receipt in $receipts; do
  package="${receipt%%:*}"; digest="${receipt#*:}"
  set -- /var/cache/apt/archives/${package}_*_${architecture}.deb
  [ "$#" -eq 1 ]
  printf '%s  %s\n' "$digest" "$1" | sha256sum -c -
done
apt-get install -y --reinstall --no-install-recommends $packages
for package in $packages; do
  name="${package%%=*}"; version="${package#*=}"
  [ "$(dpkg-query -W -f='${Version}' "$name")" = "$version" ]
done
rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*
