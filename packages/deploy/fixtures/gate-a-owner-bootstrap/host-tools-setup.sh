#!/bin/bash
# Run only as the fresh normal kos engine owner; never root or administrator.
set -euo pipefail
umask 077
if ! [ "$(id -un)" = kos ]; then
  echo "homebrew_owner_guard_refused: STOP before installation." >&2
  exit 2
fi
if ! [ "$(id -u)" -ne 0 ]; then
  echo "homebrew_owner_guard_refused: STOP before installation." >&2
  exit 2
fi
if ! [ "$HOME" = /home/kos ]; then
  echo "homebrew_owner_guard_refused: STOP before installation." >&2
  exit 2
fi
if ! [ "${USER:?}" = kos ]; then
  echo "homebrew_owner_guard_refused: STOP before installation." >&2
  exit 2
fi
if ! [ "${LOGNAME:?}" = kos ]; then
  echo "homebrew_owner_guard_refused: STOP before installation." >&2
  exit 2
fi
if ! [ ! -L /home/linuxbrew/.linuxbrew ]; then
  echo "homebrew_owner_guard_refused: STOP before installation." >&2
  exit 2
fi
if ! [ -d /home/linuxbrew/.linuxbrew ]; then
  echo "homebrew_owner_guard_refused: STOP before installation." >&2
  exit 2
fi
if ! [ "$(stat -c %u /home/linuxbrew/.linuxbrew)" = "$(id -u)" ]; then
  echo "homebrew_owner_guard_refused: STOP before installation." >&2
  exit 2
fi
if ! [ -w /home/linuxbrew/.linuxbrew ]; then
  echo "homebrew_owner_guard_refused: STOP before installation." >&2
  exit 2
fi
if ! [ ! -e /home/linuxbrew/.linuxbrew/bin/brew ]; then
  echo "homebrew_owner_guard_refused: STOP before installation." >&2
  exit 2
fi
if ! [ ! -L /home/linuxbrew/.linuxbrew/bin/brew ]; then
  echo "homebrew_owner_guard_refused: STOP before installation." >&2
  exit 2
fi
HOMEBREW_NO_SUDO=1 NONINTERACTIVE=1 HOMEBREW_NO_ANALYTICS=1 bash /home/kos/gate-a-bootstrap/homebrew-install.sh
HOMEBREW_NO_ANALYTICS=1 /home/linuxbrew/.linuxbrew/bin/brew install podman conmon crun passt fuse-overlayfs
/home/linuxbrew/.linuxbrew/bin/brew list --versions podman conmon crun passt fuse-overlayfs

# Owner-helper preflight before the unchanged accepted runtime owner block.
for kos_helper_path in /home/linuxbrew/.linuxbrew/opt/podman/bin/podman /home/linuxbrew/.linuxbrew/opt/conmon/bin/conmon /home/linuxbrew/.linuxbrew/opt/crun/bin/crun /home/linuxbrew/.linuxbrew/opt/passt/bin/pasta /home/linuxbrew/.linuxbrew/opt/fuse-overlayfs/bin/fuse-overlayfs /home/linuxbrew/.linuxbrew/opt/podman/libexec/podman/rootlessport /home/linuxbrew/.linuxbrew/opt/podman/libexec/podman/netavark /home/linuxbrew/.linuxbrew/opt/podman/libexec/podman/aardvark-dns; do
  if ! [ -x "$kos_helper_path" ]; then
    printf 'homebrew_owner_helper_unavailable: %s; STOP before block 3.\n' "$kos_helper_path" >&2
    exit 2
  fi
  kos_helper_resolved=$(readlink -f "$kos_helper_path")
  kos_helper_formula=${kos_helper_path#/home/linuxbrew/.linuxbrew/opt/}
  kos_helper_formula=${kos_helper_formula%%/*}
  case "$kos_helper_resolved" in
    "/home/linuxbrew/.linuxbrew/Cellar/$kos_helper_formula/"*) ;;
    *) echo 'homebrew_owner_helper_unavailable: helper outside its formula Cellar; STOP.' >&2; exit 2 ;;
  esac
  if ! [ "$(stat -c %u "$kos_helper_resolved")" = "$(id -u)" ]; then
    echo 'homebrew_owner_helper_unavailable: foreign-owned helper; STOP.' >&2
    exit 2
  fi
  stat -c '%u %g %a %s %n' "$kos_helper_resolved"
done
for kos_program in podman conmon crun; do
  "/home/linuxbrew/.linuxbrew/opt/$kos_program/bin/$kos_program" --version
done
/home/linuxbrew/.linuxbrew/opt/passt/bin/pasta --version
/home/linuxbrew/.linuxbrew/opt/fuse-overlayfs/bin/fuse-overlayfs --version
