#!/bin/bash
# Run once as rocky with the AMI's existing sudo authority.
set -euo pipefail
umask 077
[ "$(id -un)" = rocky ] && ! id kos >/dev/null 2>&1
sudo useradd --create-home --shell /bin/bash kos
sudo chmod 700 /home/kos
sudo install -d -o kos -g kos -m 700 /home/kos/.ssh
sudo install -o kos -g kos -m 600 /home/rocky/.ssh/authorized_keys /home/kos/.ssh/authorized_keys
sudo loginctl enable-linger kos
sudo systemctl start "user@$(id -u kos).service"
id kos
awk -F: '$1=="kos" {print FILENAME":"$0}' /etc/subuid /etc/subgid
sudo stat -c '%u %a %n' /home/kos "/run/user/$(id -u kos)"
sudo test ! -e /home/kos/.config
sudo test ! -e /home/kos/.local/share/containers/storage
