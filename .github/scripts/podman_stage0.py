"""Hosted-runner qualification only: rootless inventory and finite cgroup controls."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import uuid


IMAGE = "docker.io/library/python:3.14.7-slim-trixie@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6"
LIMITS = """
from pathlib import Path
import json,os
c=Path('/sys/fs/cgroup')
limits={n:(c/n).read_text().strip() for n in ['memory.max','memory.swap.max','cpu.max']}
assert limits['memory.max']=='1073741824',limits
assert limits['memory.swap.max']=='0',limits
quota,period=map(int,limits['cpu.max'].split());assert quota/period==2,limits
assert os.getuid()==10001 and os.getgid()==10001
print(json.dumps({'limits':limits,'uid':os.getuid(),'gid':os.getgid()}),flush=True)
"""
CPU = LIMITS + """
import subprocess,sys,time
def stats():return {a:int(b) for a,b in [x.split() for x in (c/'cpu.stat').read_text().splitlines()]}
before=stats();start=time.monotonic()
code='import time; end=time.monotonic()+8; x=1\\nwhile time.monotonic()<end: x=(x*x+1)%2147483647'
children=[subprocess.Popen([sys.executable,'-c',code]) for _ in range(4)]
assert all(p.wait()==0 for p in children)
wall=time.monotonic()-start;after=stats();used=(after['usage_usec']-before['usage_usec'])/1e6
throttled=after['nr_throttled']-before['nr_throttled']
assert throttled>0,(before,after)
assert used/wall<=2.25,(used,wall)
print(json.dumps({'cpu_seconds':used,'wall_seconds':wall,'cpu_ratio':used/wall,'throttled_periods':throttled,'before':before,'after':after}),flush=True)
"""
ALLOCATOR = "from pathlib import Path; import json\ntry: Path('/proc/self/oom_score_adj').write_text('1000')\nexcept PermissionError: pass\nchunks=[]\nfor i in range(64):\n chunk=bytearray(32*1024*1024)\n for offset in range(0,len(chunk),4096): chunk[offset]=1\n chunks.append(chunk)\n print(json.dumps({'allocated_bytes':sum(map(len,chunks))}),flush=True)"
OOM = LIMITS + 'import subprocess,sys\ndef events():return {a:int(b) for a,b in [x.split() for x in (c/\'memory.events\').read_text().splitlines()]}\nbefore=events();peak_before=int((c/\'memory.peak\').read_text());group=(c/\'memory.oom.group\').read_text().strip()\nassert group==\'0\', \'observer cannot survive group OOM policy\'\nallocator="from pathlib import Path; import json\\ntry: Path(\'/proc/self/oom_score_adj\').write_text(\'1000\')\\nexcept PermissionError: pass\\nchunks=[]\\nfor i in range(64):\\n chunk=bytearray(32*1024*1024)\\n for offset in range(0,len(chunk),4096): chunk[offset]=1\\n chunks.append(chunk)\\n print(json.dumps({\'allocated_bytes\':sum(map(len,chunks))}),flush=True)"\nchild=subprocess.run([sys.executable,\'-c\',allocator],text=True,capture_output=True,timeout=30)\nafter=events();peak=int((c/\'memory.peak\').read_text());limit=int(limits[\'memory.max\'])\ndelta={key:after.get(key,0)-before.get(key,0) for key in after}\nproof={\'observer_survived\':True,\'observer_pid\':os.getpid(),\'child_exit\':child.returncode,\'before\':before,\'after\':after,\'delta\':delta,\'memory_peak_before\':peak_before,\'memory_peak\':peak,\'memory_limit\':limit,\'child_stdout\':child.stdout,\'child_stderr\':child.stderr,\'oom_group\':group}\nprint(json.dumps(proof),flush=True)\nassert child.returncode==-9,proof\nassert delta.get(\'oom_kill\',0)>0,proof\nassert peak<=limit,proof\n'


def probe(output):
    compile(CPU, "<cpu-probe>", "exec")
    compile(OOM, "<oom-observer>", "exec")
    compile(ALLOCATOR, "<allocator-control>", "exec")
    output.mkdir(parents=True, exist_ok=True)
    run_id = os.environ.get("GITHUB_RUN_ID", "local") + "-" + uuid.uuid4().hex[:12]
    labels = {"cortex.worker": "ren-cx", "cortex.stage0": run_id}
    owned = {}
    candidates = set()
    receipt = {"run_id": run_id, "stage": "hosted-rootless-only", "image": IMAGE,
               "database_created": False, "admissions": [], "commands": []}

    def command(args, timeout=90):
        p = subprocess.run(["podman", *args], capture_output=True, text=True, timeout=timeout)
        receipt["commands"].append({"args": args[:4], "exit": p.returncode,
                                    "stdout": p.stdout, "stderr": p.stderr})
        return p

    def admission():
        raw = Path("/proc/meminfo").read_text()
        fields = {line.split(":")[0]: int(line.split()[1]) for line in raw.splitlines()}
        free = fields["MemAvailable"] * 100 / fields["MemTotal"]
        receipt["admissions"].append({"available_percent": free, "raw": raw})
        if free < 35:
            raise RuntimeError("NOT_PROVEN: available memory below35percent")

    def inspect(name):
        p = command(["inspect", name])
        if p.returncode:
            raise RuntimeError("NOT_PROVEN: probe container inspection failed")
        row = json.loads(p.stdout)[0]
        if row["Config"]["Labels"].get("cortex.stage0") != run_id:
            raise RuntimeError("ownership mismatch; refusing operation")
        owned[name] = row["Id"]
        return row

    def run(label, script):
        admission()
        name = "kaidera-test-stage0-" + run_id + "-" + label
        args = ["run", "--name", name, "--pull=never", "--network=none", "--user=10001:10001",
                "--read-only", "--memory=1g", "--memory-swap=1g", "--cpus=2", "--pids-limit=128",
                "--cap-drop=ALL", "--security-opt=no-new-privileges", "--tmpfs=/tmp:rw,size=32m,mode=1777"]
        for key, value in labels.items():
            args.extend(["--label", key + "=" + value])
        args.extend([IMAGE, "python", "-c", script])
        candidates.add(name)
        result = command(args, timeout=45)
        if command(["container", "exists", name]).returncode == 0:
            row = inspect(name)
            (output / (label + "-inspect.json")).write_text(json.dumps(row, indent=2) + "\n")
        else:
            row = None
        (output / (label + ".txt")).write_text(result.stdout + result.stderr)
        data = {"exit": result.returncode, "stdout": result.stdout, "stderr": result.stderr,
                "state": row["State"] if row else None}
        receipt[label] = data
        if name in owned:
            inspect(name)
            removed = command(["rm", owned[name]])
            if removed.returncode:
                raise RuntimeError("probe cleanup failed")
            owned.pop(name)
            candidates.discard(name)
        return data

    try:
        if os.getuid() == 0:
            raise RuntimeError("NOT_PROVEN: runner is root")
        info = command(["info", "--format=json"])
        if info.returncode:
            raise RuntimeError("NOT_PROVEN: podman info failed")
        value = json.loads(info.stdout)
        receipt["podman_info"] = value
        receipt["host_uid"] = os.getuid()
        receipt["host_gid"] = os.getgid()
        for name in ["/etc/subuid", "/etc/subgid"]:
            receipt[name] = Path(name).read_text()
        if not value["host"]["security"]["rootless"] or value["host"]["cgroupVersion"] != "v2":
            raise RuntimeError("NOT_PROVEN: rootless/cgroupv2 prerequisites failed")
        version = command(["version", "--format={{.Client.Version}}"])
        receipt["version"] = version.stdout.strip()
        if version.returncode or receipt["version"] != "4.9.3":
            raise RuntimeError("NOT_PROVEN: hosted Podman version differs from accepted4.9.3")
        admission()
        pulled = command(["pull", IMAGE], timeout=120)
        if pulled.returncode:
            raise RuntimeError("NOT_PROVEN: immutable probe image unavailable")
        positive = run("cpu", CPU)
        if positive["exit"] != 0:
            raise RuntimeError("NOT_PROVEN: finite limit/CPU-throttle probe failed")
        negative = run("oom", OOM)
        events = [json.loads(line) for line in negative["stdout"].splitlines()
                  if line.startswith("{")]
        evidence = next((event for event in events if event.get("observer_survived")), None)
        if negative["exit"] != 0 or not evidence or evidence["delta"].get("oom_kill", 0) <= 0:
            raise RuntimeError("NOT_PROVEN: surviving observer did not record a kernel OOMkill")
        if evidence["memory_peak"] > evidence["memory_limit"]:
            raise RuntimeError("NOT_PROVEN: kernel memory peak exceeded accepted limit")
        receipt["status"] = "PASS"
        receipt["over_limit_control"] = "kernel-oom-kill"
        receipt["kernel_memory_evidence"] = evidence
    except BaseException as exc:
        receipt["status"] = "NOT_PROVEN"
        receipt["reason"] = type(exc).__name__ + ": " + str(exc)
    finally:
        for name in candidates:
            if name not in owned and command(["container", "exists", name]).returncode == 0:
                inspect(name)
        for name in list(owned):
            inspect(name)
            removed = command(["rm", "-f", owned[name]])
            if removed.returncode:
                receipt["status"] = "NOT_PROVEN"
                receipt["reason"] = "owned probe cleanup failed"
            else:
                owned.pop(name)
        inventory = command(["ps", "-a", "--filter", "label=cortex.stage0=" + run_id, "--format=json"])
        receipt["cleanup_verified"] = inventory.returncode == 0 and json.loads(inventory.stdout) == []
        if not receipt["cleanup_verified"]:
            receipt["status"] = "NOT_PROVEN"
        (output / "stage0.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps({k: receipt.get(k) for k in ["status", "reason", "version", "over_limit_control", "cleanup_verified"]}))
    return 0 if receipt["status"] == "PASS" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    raise SystemExit(probe(parser.parse_args().output))
