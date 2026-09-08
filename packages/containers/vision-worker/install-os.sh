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
packages='curl=8.14.1-2+deb13u4 ca-certificates=20250419 libvulkan1=1.4.309.0-1 libopenblas0=0.3.29+ds-3'
# Word splitting is intentional: this fixed reviewed list contains no user input.
apt-get install -y --download-only --reinstall --no-install-recommends $packages
architecture="$(dpkg --print-architecture)"
case "$architecture" in
  amd64) receipts='curl:7bd23df36e65ca32a5bbd81f67ca311b9e0c122212c70d0733f4e45d745789d2 libvulkan1:f47da79cd140264fe21cceb08bc87a71bc7fec05819e4a421f3f518d21101a37 libopenblas0:d76720bc878c6085096547e45797c6a6a5aea98c729a05d18d68f1a3e122e6bb' ;;
  arm64) receipts='curl:9881fbb76558c576488c90e0a1cb1cc2d2154b1e39a70dd44e2b736200146afe libvulkan1:f5f576d8b9e5703702a9f3254c8c5d130bef17da9db4aecd46ba86cec7a08930 libopenblas0:a862b1d762dff98bbd78d7b70e1bfbf2eefa6cf6c80c9f7777116b847dcf9946' ;;
  *) exit 1 ;;
esac
for receipt in $receipts; do
  package="${receipt%%:*}"; digest="${receipt#*:}"
  set -- /var/cache/apt/archives/${package}_*_${architecture}.deb
  [ "$#" -eq 1 ]
  printf '%s  %s\n' "$digest" "$1" | sha256sum -c -
done
set -- /var/cache/apt/archives/ca-certificates_*_all.deb
[ "$#" -eq 1 ]
printf '%s  %s\n' ef590f89563aa4b46c8260d49d1cea0fc1b181d19e8df3782694706adf05c184 "$1" | sha256sum -c -
apt-get install -y --reinstall --no-install-recommends $packages
for package in $packages; do
  name="${package%%=*}"; version="${package#*=}"
  [ "$(dpkg-query -W -f='${Version}' "$name")" = "$version" ]
done
rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*
