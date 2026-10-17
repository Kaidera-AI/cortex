"""Causal controls for signature-before-effects: each mutant of sign/write_release.py, sign/verify.sh or install.sh must be killed by an assertion of the
UNCHANGED test_sign.py; the healthy and the restored source pass. Run: python3 mutate_sign.py [out.json]  (works on a temp copy; the source is untouched)."""
import json, os, shutil, subprocess, sys, tempfile
from pathlib import Path
HERE = Path(__file__).resolve().parent
INSTALL = HERE.parent
MUTANTS = [
    ('verify_skips_signature_check', 'sign/verify.sh', 'minisign -V -q -p "$keyfile" -m "$dir/SHA256SUMS" >/dev/null 2>&1 ||', 'true ||'),
    ('verify_skips_digest_compare', 'sign/verify.sh', '[ "$(sha_of "$dir/$name")" = "$want" ] ||', 'true ||'),
    ('verify_checks_other_platform_too', 'sign/verify.sh', '*linux-arm64*|*linux-amd64*) case $name in *"$platform"*) ;; *) continue ;; esac ;;', '*linux-arm64*|*linux-amd64*) ;;'),
    ('verify_ignores_missing_archive', 'sign/verify.sh', '[ -f "$dir/$name" ] || { verify_refuse "artifact_missing:$name"; return 2; }', ':'),
    ('verify_accepts_unknown_platform', 'sign/verify.sh', '|| { verify_refuse platform_not_in_release; return 2; }', '|| true'),
    ('install_does_not_verify', 'install.sh', 'else verify_release "$release_dir" --platform "$platform" || exit 2; fi', 'else true; fi'),
    ('install_ignores_trust_key_refusal', 'install.sh', '--trust-key-file "$keyfile" --platform "$platform" || exit 2', '--trust-key-file "$keyfile" --platform "$platform" || true'),
    ('install_runs_effects_without_wiring_check', 'install.sh', 'for phase in $UNWIRED; do refuse "install_not_wired:$phase"; done', ':'),
    ('install_floor_off', 'install.sh', 'refuse "podman_too_old:$found"', ':'),
    ('install_loads_every_platform', 'install.sh', 'for role in core api; do', 'for role in core api; do platform=linux-amd64; podman load -q -i "$release_dir/cortex-$role-linux-amd64.oci.tar" >/dev/null;'),
    ('writer_leaves_release_json_out_of_sums', 'sign/write_release.py', "    sums['release.json'] = hashlib.sha256(release).hexdigest()\n", ''),
    ('writer_accepts_index_files_in_payload', 'sign/write_release.py', "    if bad:\n        refuse('payload_names_index_file:' + ','.join(bad))\n", ''),
]


def run(tree):
    r = subprocess.run([sys.executable, '-m', 'unittest', 'test_sign'], cwd=tree / 'tests', capture_output=True, text=True, env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
    fails = [l.split(' (')[0] for l in r.stderr.splitlines() if l.startswith(('FAIL:', 'ERROR:'))]
    return r.returncode, fails, (r.stderr.strip().splitlines() or [''])[-1]


def main():
    results = [dict(case='healthy', phase='healthy', **dict(zip(('exit', 'failures', 'tail'), run(INSTALL))))]
    for name, rel, old, new in MUTANTS:
        src = (INSTALL / rel).read_text()
        assert src.count(old) == 1, name
        tmp = Path(tempfile.mkdtemp(prefix='nix-mut-'))
        deep = tmp / 'x/next/release/ship/install'
        deep.parent.mkdir(parents=True)
        shutil.copytree(INSTALL, deep, ignore=shutil.ignore_patterns('__pycache__'))
        for d in ('contracts', 'src', 'schema'):
            os.symlink(INSTALL.parents[2] / d, deep.parents[2] / d)
        (deep / rel).write_text(src.replace(old, new, 1))
        code, fails, tail = run(deep)
        results.append(dict(case=name, phase='mutant', exit=code, failures=fails, tail=tail, killed=code != 0 and any(not f.startswith('ERROR') for f in fails)))
        shutil.rmtree(tmp, ignore_errors=True)
    results.append(dict(case='restored', phase='restored', **dict(zip(('exit', 'failures', 'tail'), run(INSTALL)))))
    if len(sys.argv) > 1:
        Path(sys.argv[1]).write_text(json.dumps(results, indent=1) + '\n')
    for r in results:
        print(r['case'], r['phase'], 'exit', r['exit'], 'KILLED by ' + ', '.join(r['failures'][:2]) if r.get('killed') else ('' if r['phase'] != 'mutant' else 'SURVIVED'))
    ok = results[0]['exit'] == 0 and results[-1]['exit'] == 0 and all(r['killed'] for r in results if r['phase'] == 'mutant')
    print('ALL MUTANTS KILLED, healthy and restored pass' if ok else 'CAUSAL CONTROL FAILED')
    sys.exit(0 if ok else 1)


main()
