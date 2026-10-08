#!/bin/bash
# Run as the AMI's normal rocky account; keep the new kos HOME untouched.
set -euo pipefail
umask 077
[ "$(id -un)" = rocky ] && [ "$(id -u)" -ne 0 ]
[ ! -e /home/linuxbrew/.linuxbrew ]
NONINTERACTIVE=1 HOMEBREW_NO_ANALYTICS=1 bash /home/rocky/gate-a-bootstrap/homebrew-install.sh
HOMEBREW_NO_ANALYTICS=1 /home/linuxbrew/.linuxbrew/bin/brew install podman conmon crun passt fuse-overlayfs
/home/linuxbrew/.linuxbrew/bin/brew list --versions podman conmon crun passt fuse-overlayfs
/home/linuxbrew/.linuxbrew/bin/podman --version
