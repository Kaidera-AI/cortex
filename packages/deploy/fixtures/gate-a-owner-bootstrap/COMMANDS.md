# R426 fresh15aff361 Gate A commands — TEST ONLY

Application `15aff361c9835f24d1e5eedba4be9b5f5168a87f`, image producer `350a0da3db23841a60def102355f732f43f185e0`.
All seven roles are from the fresh serialized run; partial failed attempts remain separate.
No production signature or runtime qualification is claimed by this preparation.
The ledger binds43 transfer inputs, including22 actual release artifacts. The private signing key was memory-only.
The manifest's engine-only observation6.1.3 is retained from the retired host; the fresh target's engine and source qualification must be measured separately.
Ren-cx binds the final run sheet once before execution. Fresh host/fingerprint/UID/installation UUID are observed facts.

## Bootstrap and private transfer

After the r419 instance and AWS-console SSH binding are verified, on the Mac:

```bash
python3 /Users/amadmalik/DevVault/helix/output/KOS/r426-15aff361-rebuild-20261008/gate-a-assets/prepare-host.py --host "$gate_a_host" --known-hosts "$gate_a_known_hosts" --key /Users/amadmalik/.ssh/kaidera-k8s.pem
python3 /Users/amadmalik/DevVault/helix/output/KOS/r426-15aff361-rebuild-20261008/gate-a-assets/transfer-assets.py --host "$gate_a_host" --known-hosts "$gate_a_known_hosts" --key /Users/amadmalik/.ssh/kaidera-k8s.pem --files /Users/amadmalik/DevVault/helix/output/KOS/r426-15aff361-rebuild-20261008/gate-a-assets/transfer-files.json
ssh -i /Users/amadmalik/.ssh/kaidera-k8s.pem -o BatchMode=yes -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=$gate_a_known_hosts" "kos@$gate_a_host" 'cd /home/kos/cortex-gate-a/assets && sha256sum -c -' < /Users/amadmalik/DevVault/helix/output/KOS/r426-15aff361-rebuild-20261008/gate-a-assets/SHA256SUMS.TEST
```

Private transfer is the admitted TEST-only replacement for the guide download block; no public URL is invented.
Administrator preparation runs as rocky. The Homebrew installer and brew install run as kos; the owner-helper preflight must pass before block 3. All runtime target commands run as kos.
Fresh capacity requires4vCPU/16GiB, encrypted80GiB root, private bare HOME/config/store, unused8501/projectcortex; actual observations are retained before setup.

## Verify, extract, owner setup and cache all seven images

Target as kos:

```bash
set -euo pipefail
umask 077
cortex_assets=/home/kos/cortex-gate-a/assets
cd "$cortex_assets"
printf '%s\n' '2c74dffcc1c9a5ee55957c60971998ace2b89f22585631594ec2152c588af8db  minisign-linux-amd64' | sha256sum -c -
chmod 500 minisign-linux-amd64
python3 "$cortex_assets/verify-assets.py" --test-dir "$cortex_assets" --minisign "$cortex_assets/minisign-linux-amd64" --artifact-root "$cortex_assets"
python3 "$cortex_assets/extract-assets.py" --archive "$cortex_assets/cortex-host-tools-0.1.003-manual.1-linux-amd64.tar.gz" --sha256 3a14745305d9b31a56bfbf8ffdd194d71f5250cc8fa966ad275f9a7c98e285ec --output /home/kos/cortex-gate-a/tools
python3 "$cortex_assets/extract-assets.py" --archive "$cortex_assets/cortex-0.1.003-manual.1-15aff361c983-prebuilt-linux-amd64.tar.gz" --sha256 abda29cfbb8015db4e5ec17a93ea3b819dc5d6781cca07f9c94327fb6ca23b47 --output /home/kos/cortex-gate-a/payload
python3 "$cortex_assets/extract-assets.py" --archive "$cortex_assets/TEST-kos-consumer-bf5fc45e.tar.gz" --sha256 57ea78ae671a23a6597b64063149c385a25b3b0c6dabce55991d82d9332eba77 --output /home/kos/cortex-gate-a/kos-consumer
export PATH="/home/kos/cortex-gate-a/tools/cortex-host-tools/bin:/home/linuxbrew/.linuxbrew/bin:/usr/bin:/bin"
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
unset CORTEX_SERVICE_AUTH_TRUSTED_NETWORKS
bash "$cortex_assets/accepted-owner-setup.sh"
bash "$cortex_assets/load-images.sh"
python3 "$cortex_assets/verify-cached-images.py" --payload-dir /home/kos/cortex-gate-a/payload --state-dir /home/kos/cortex-gate-a/state --runtime-sha256 cfee1b0a59ee5c3c3eceb57b71634dc0b80abf7d2c38bad7a2a76c255172e40e
```

Run accepted guide host inspection exactly. Require SELinux Enforcing, actual engine >=signed minimum6.0.2 and denylist policy, subordinate mappings, user runtime/linger and all expected stock opt helpers. Archive hashes differ from platform-manifest digests. Cache check uses allow_pull=False; no registry fetch for application images after load.

## Source-owned authority and labels

```bash
cortex_payload=/home/kos/cortex-gate-a/payload
cortex_state=/home/kos/cortex-gate-a/state
cortex_runtime="$cortex_payload/packages/deploy/cortex-runtime"
cortex_credentials="$HOME/.kaidera-os/cortex-auth/KOS-Cortex"
cortex_project=cortex-standalone-canary
python3 "$cortex_runtime" provider-setup --project cortex --state-dir "$cortex_state" --payload-dir "$cortex_payload" --provider-home "$HOME/.openkai"
( set -C; python3 "$cortex_runtime" provider-labels-inventory --project cortex --state-dir "$cortex_state" --payload-dir "$cortex_payload" > "$cortex_state/provider-label-inventory.json" )
```

Inspect that exact inventory. If status is not_applicable, do not manufacture a hash or run apply. Otherwise bind cortex_label_sha from the observed inventory_sha256, then:

```bash
python3 "$cortex_runtime" provider-labels-apply --project cortex --state-dir "$cortex_state" --payload-dir "$cortex_payload" --label-inventory-sha256 "$cortex_label_sha"
```

No hand-authored.env, authority, runtime-owner receipt or provider value. New authority uses only the source-pinned public empty/commented template. Default loopback remains until the actual peer is measured; differing peer stops dependent auth until an exact receipt-backed ruling, never a broad CIDR.

## Runtime, single enrollment and authenticated readiness

```bash
python3 "$cortex_runtime" up --project cortex --state-dir "$cortex_state" --payload-dir "$cortex_payload" --api-port 8501 --credential-dir "$cortex_credentials" --credential-project "$cortex_project"
python3 "$cortex_runtime" enroll-console --owner-engine local --project cortex --state-dir "$cortex_state" --payload-dir "$cortex_payload" --api-port 8501 --credential-dir "$cortex_credentials" --credential-project "$cortex_project"
python3 "$cortex_runtime" check --project cortex --state-dir "$cortex_state" --payload-dir "$cortex_payload" --api-port 8501 --credential-dir "$cortex_credentials" --credential-project "$cortex_project"
```

First up must return started:true,healthy:false,enrollment-required. Enrollment is one attempt; uncertainty preserves barrier/staging and stops. UUID is emitted metadata, never guessed. Verify owner-control volume10001:10001/0700, API nonroot/read-only, private token custody, actual SQL console registration and enforced RLS. Tokens/admin/provider secrets never enter argv or logs.

## TEST writer, unchanged KOS consumer, denials and ordinary six-service restart

Bind cortex_installation_id only from the successful enrollment metadata:

```bash
python3 "$cortex_assets/TEST-writer.py" --payload-dir "$cortex_payload" --test-dir "$cortex_assets" --home "$HOME" --manifest "$cortex_assets/release.json" --signature "$cortex_assets/release.json.minisig" --api-url http://127.0.0.1:8501 --installation-id "$cortex_installation_id" --credential-dir "$cortex_credentials" --project "$cortex_project"
python3 "$cortex_assets/TEST-consumer.py" --kos-source /home/kos/cortex-gate-a/kos-consumer --test-dir "$cortex_assets" --home "$HOME"
python3 "$cortex_assets/denials.py"
python3 "$cortex_assets/restart-runtime.py" --payload-dir "$cortex_payload" --state-dir "$cortex_state" --runtime-sha256 cfee1b0a59ee5c3c3eceb57b71634dc0b80abf7d2c38bad7a2a76c255172e40e
python3 "$cortex_assets/TEST-consumer.py" --kos-source /home/kos/cortex-gate-a/kos-consumer --test-dir "$cortex_assets" --home "$HOME"
python3 "$cortex_assets/denials.py"
```

Actual writer logic receives only an in-memory public TEST-key override; production source/key unchanged on disk. Unchanged BF KOS consumer checks its source byte map before/after and rejects TEST with production trust. Restart covers six long-running services only: no TLS initializer, migration, enrollment replay or reset. Before/after identity/images/schema and authenticated readiness agree.

## Independent real SQL binding

Already executed on exact15aff361, same unchanged runner, new owned private PG18 job, not installed runtime DB:

```bash
/tmp/ren-kos-r330-b11-20261007-19bc4c/env/bin/python /home/rocky/ren-kos-r426-15aff361-build-20261008/run-real-sql.py --source /home/rocky/ren-kos-r426-15aff361-build-20261008/application --job /home/rocky/ren-kos-r426-15aff361-sql-20261008 --pg-root /tmp/ren-kos-r330-b11-20261007-19bc4c/pg --test-python /tmp/ren-kos-r330-b11-20261007-19bc4c/env/bin/python
```

Receipt `/Users/amadmalik/DevVault/helix/output/KOS/r426-15aff361-rebuild-20261008/sql-receipts/`:4 real cases+11 guards=15PASS,0FAIL,0ERROR,0SKIP,exit0; source/test before-after hashes match and cluster stopped. The command identifies the completed job, not permission to rerun that existing directory. Earlier460 SQL RED stays retained. Fresh target native SQL/RLS registration is separately observed during runtime qualification; destructive fixture tests never use that installed database.

## Closure

Save/hash-verify receipts locally before terminating the fresh r419 instance and record actual ID, instance-hour price and measured compute/disk/IPv4 cost. Any failed step is RED and stops dependent execution; no retry masking. Vera reviews the frozen actual packet next; CTO Cortex signature, fresh Gate B, joint installed rehearsal, publication and soak remain separate gates.
