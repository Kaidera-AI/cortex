#!/usr/bin/env python3
"""r419 native owner/stock Homebrew bootstrap; no instance launch here."""
import argparse,subprocess
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--host',required=True);p.add_argument('--known-hosts',type=Path,required=True);p.add_argument('--key',type=Path,required=True);a=p.parse_args()
opts=['-i',str(a.key),'-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','UserKnownHostsFile='+str(a.known_hosts)];target='rocky@'+a.host;b=Path(__file__).parent
subprocess.run(['ssh',*opts,target,'test "$(uname -m)" = x86_64 && test "$(getenforce)" = Enforcing && umask 077 && test ! -e /home/rocky/gate-a-bootstrap && mkdir -m 700 /home/rocky/gate-a-bootstrap'],check=True)
for n in ['homebrew-install.sh','new-owner-setup.sh','host-tools-setup.sh']:subprocess.run(['scp',*opts,str(b/n),target+':/home/rocky/gate-a-bootstrap/'],check=True)
for n in ['new-owner-setup.sh','host-tools-setup.sh']:subprocess.run(['ssh',*opts,target,'bash /home/rocky/gate-a-bootstrap/'+n],check=True)
