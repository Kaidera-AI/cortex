"""Read-only Mac/active Podman VM admission; no resource creation or key access."""
import json
import re
import subprocess

def output(args):
    return subprocess.check_output(args,text=True,timeout=60)
mac_raw=output(['memory_pressure','-Q'])
mac=int(re.search(r'System-wide memory free percentage:\s*(\d+)%',mac_raw).group(1))
machines=json.loads(output(['podman','machine','list','--format','json']))
active=[m for m in machines if m.get('Running') and m.get('Default')]
assert len(active)==1,'active default machine must be explicit'
name=active[0]['Name']
raw=output(['podman','machine','ssh',name,'cat /proc/meminfo'])
values={line.split(':',1)[0]:int(line.split(':',1)[1].split()[0]) for line in raw.splitlines() if line.startswith(('MemTotal:','MemAvailable:'))}
vm=100*values['MemAvailable']/values['MemTotal']
passed=mac>=35 and vm>=35
print(json.dumps({'passed':passed,'floor_percent':35,'mac_free_percent':mac,
                  'vm':name,'vm_free_percent':vm,'vm_meminfo_kB':values,
                  'mac_measurement':mac_raw,'resource_operations':0}))
raise SystemExit(0 if passed else 1)
