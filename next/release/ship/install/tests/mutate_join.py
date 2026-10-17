"""Causal controls for HALE-A1-NIX-JOIN-001 / CI-001: each mutant of roles.py must be killed by an assertion of the UNCHANGED test_roles.py, the healthy
source and the restored source pass. Run: python3 mutate_join.py [out.json]   (writes only a temp copy of the module; the source is restored)."""
import json, os, shutil, subprocess, sys, tempfile
from pathlib import Path
HERE = Path(__file__).resolve().parent
SRC = HERE.parent / 'roles.py'
MUTANTS = {
    'join_snapshot_check_removed': ("    if any(other != shared[0] for other in shared[1:]):\n        raise RoleRefusal('role_config_conflict:snapshot')\n", ''),
    'login_collision_check_removed': ("    if len(set(logins.values())) != len(logins):\n        raise RoleRefusal('role_config_collision:login')\n", ''),
    'secret_collision_check_removed': ("    if len(set(paths)) != len(paths):\n        raise RoleRefusal('role_config_collision:secret')\n", ''),
    'snapshot_ignores_non_vault_fields': ("{k: v for k, v in vars(cfg).items() if k not in ('mode', 'roles')}", "{k: v for k, v in vars(cfg).items() if k == 'vault_root'}"),
    'collision_uses_only_first_view': ("    if len(set(logins.values())) != len(logins):", "    if len(set(list(logins.values())[:1])) != 1:"),
}
def run(tree):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    r = subprocess.run([sys.executable, '-m', 'unittest', 'test_roles'], cwd=tree / 'tests', capture_output=True, text=True, env=env)
    out = r.stderr
    return r.returncode, [l for l in out.splitlines() if l.startswith(('FAIL:', 'ERROR:'))], out.strip().splitlines()[-1] if out.strip() else ''
def main():
    original = SRC.read_text(); results = []
    for phase in ('healthy',):
        results.append(dict(case='healthy', phase=phase, **dict(zip(('exit', 'failures', 'tail'), run(SRC.parent)))))
    for name, (old, new) in MUTANTS.items():
        assert original.count(old) == 1, name
        tree = Path(tempfile.mkdtemp(prefix='nix-mut-'))
        shutil.copytree(SRC.parent, tree / 'install', ignore=shutil.ignore_patterns('__pycache__'))
        # the tests locate NEXT by relative depth: mirror the real depth
        deep = tree / 'x/next/release/ship/install'; deep.parent.mkdir(parents=True)
        shutil.move(str(tree / 'install'), str(deep))
        for rel in ('contracts', 'src', 'schema'):
            os.symlink(HERE.parents[3] / rel, deep.parents[2] / rel)
        (deep / 'roles.py').write_text(original.replace(old, new, 1))
        code, fails, tail = run(deep)
        results.append(dict(case=name, phase='mutant', exit=code, failures=fails, tail=tail, killed=code != 0 and any('FAIL:' in f for f in fails)))
        shutil.rmtree(tree, ignore_errors=True)
    results.append(dict(case='restored', phase='restored', **dict(zip(('exit', 'failures', 'tail'), run(SRC.parent)))))
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if out: out.write_text(json.dumps(results, indent=1) + '\n')
    for r in results: print(r['case'], r['phase'], 'exit', r['exit'], 'killed' if r.get('killed') else '', len(r['failures']), 'failing assertions')
    ok = results[0]['exit'] == 0 and results[-1]['exit'] == 0 and all(r['killed'] for r in results if r['phase'] == 'mutant')
    print('ALL MUTANTS KILLED, healthy and restored pass' if ok else 'CAUSAL CONTROL FAILED'); sys.exit(0 if ok else 1)
main()
