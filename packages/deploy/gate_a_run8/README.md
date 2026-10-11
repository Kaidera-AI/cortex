# Gate A run 8 executable guard packet

Status: **NO_GO / native qualification NOT_RUN**. The checked-in B and GO are
templates. They contain unfilled final C, MAIN, production signature, trust,
source and endpoint slots, and refuse before effects. No source revision in a
test fixture is a FINAL003 selection. A local control result cannot fill a slot.

Authority: accepted plan `f7b65a635ee92beabb69ebfda6cc132e37a79e70121c3370a9c729c39e26f8c4`,
accepted restoration procedure `562a727102369072aff03100ae33bde47c06b02e447446aa6dc7fb7d505ef680`,
Kai H-D455 implementation order and 2026-10-11 19:28 control-input ruling.
Mike reviews proposal/control applicability; Vera reviews the sealed executable.
This packet does not approve itself, create a host, discover keys or sign files.

```
independently expected B/whole GO hashes
  -> original production MAIN/signature and complete signed artifact map
  -> complete executable/indirect-consumer source inventory
  -> fresh measured preallocation checks -> append-only PRE_ALLOC_CHECKED
  -> checked phase/role command -> bound invocation/public native output
  -> exclusive observation prefix -> next phase

verified delivery -> literal payload/runtime/hash -> extractor
  -> exact final-source closed installation + independent signed file map
  -> source projects predicate -> first load/provider/runtime/restart effect

console decode -> donor regex extraction -> shared classifier -> bounded wait
  -> one matching scan -> private known_hosts -> strict SSH consumer
  -> conflict/decode/timeout: durable FAILURE -> scoped retirement/verification

persistent Bash -> admitted literal -> fresh ACK + allowlisted effect
  -> durable literal prefix -> sealed install observer -> native INSTALLED input
  -> signed BF doctor -> observed install -> existing-ID restoration ARMED
  -> different boot + automatic identical effects -> REBOOT_VERIFIED -> RETIRED
```

## Local verification

From the repository root:

```sh
python3 -m unittest discover -s packages/deploy/gate_a_run8/tests -v
python3 packages/deploy/gate_a_run8/run8.py inspect-template \
  --binding packages/deploy/gate_a_run8/templates/static-binding.template.json
```

Controls use synthetic public files, a signature spy, fake SDK/host/scan/clock/
retirement boundaries, and owned local Bash processes. They never qualify
cryptographic trust, cloud, SSH, Podman, native memory limits, systemd or reboot.
The default preflight refusal is exercised through the actual CLI.

## Slot and custody rules

`templates/RULE_BOUND_SLOTS.md` describes resolution. B is immutable static
authority: no instance, UID, container ID, appdb port, ARMED or boot result is
inserted into it. Whole GO binds B plus the complete executable index without a
hash cycle. The literal is `GATE A RUN 8 GO (H-D455)`. Only the CTO's independently
received original GO can authorize a run; a template is never authorization.

Every file record is `{path, bytes, sha256}` with a physical absolute path,
regular single-link file, no group/world writes and exact measured bytes.
Staged archive members have an independently bound archive and member hash,
size and planned target path. Source membership can pass before extraction;
actual target bytes must pass after extraction. `payload_files` is the complete
file map independently derived from the signed source archive, including
`install.json`; its self-declared checksums cannot approve altered delivery.

Resolve all endpoint paths and executable/interpreter/import/shebang/indirect
consumer members against the exact accepted final source and actual controlled
operator/guest namespaces before sealing B. Run the seven source-bound positive
and negative preallocation controls, with raw facts, input hashes, context,
guard/GO and template authority and a freshness window of at most 600 seconds.
The launcher rechecks their ages immediately before its effect.

## Checked command contract

`B.commands` is indexed **by current phase, then role**. Each spec has `phase`,
`argv` (including the exact admitted executable), `shell:false`, `timeout_sec`
(1..900), and nonempty `input_digests` matching the current observation's admitted
inputs. General commands may omit public output; a native observation producer
requires `output_contract:"native-facts-json-v2"`.

Its stdout is exactly public JSON:

```json
{
  "schema": "cortex.gate-a.run8.native-facts.v2",
  "execution_scope": "native",
  "phase": "NEXT_ADMITTED_PHASE",
  "from_phase": "CURRENT_PHASE",
  "observation_predecessor": {"path": "...", "bytes": 0, "sha256": "..."},
  "input_digests": {"admitted-input": "..."},
  "facts": {}
}
```

Include the exact authority context fields as well. The dispatcher validates
destination, current predecessor, context, public nested fields and required
phase facts before publishing output. The native-output wrapper must reference
this exact captured `public_output` and its checked invocation; header claims
must equal the derived raw facts. A successful invocation cannot be reused after
the observation advances. Output refusal retains an invocation and commits
FAILURE while holding the effect's writer lock. Failed early manager/watchdog
commands preserve the allocated instance/volume for cleanup.

No command loads an environment file or inherits secret-bearing environment
variables. PATH/HOME/XDG plus admitted literals are the allowed environment.
Operational adapters are separately reviewed, complete source members of B;
their native endpoint behavior must pass the actual Gate after final GO.

### Console and SSH

The manager uses `protocol:"bounded-console-v2"`. Its sealed producer emits a
`cortex.gate-a.run8.console-attempt.v2` record with exact authority context,
allocated `instance_id`, actual ARMED local-watchdog proof, public console
output, `scan_host`, `scan_text`, and current `native_output`. Zero keys alone
retry at 60-second intervals, strictly below 600 seconds. Each fetch receives
the smaller of its timeout and the remaining wait budget. A late success is
refused. Conflict, decode failure, timeout or other protocol refusal records
FAILURE and runs the independently sealed FAILURE `retirer` followed by native
`cleanup_verifier`; only its checked RETIRED facts advance the terminal prefix.

`console_keys.py` is byte-identical to bob's shared classifier source, SHA256
`3ff62b762e6a3274e7c58394121e312519dc2f37716fd64ec5e17a9969fd4d67`,
from packet `7b6a1b82523d5b24fd8a16b8d40de9d8d11e8b18ef2153734c45db657f16b8de`.
Gate A preserves regex extraction and delegates classification to
`console_keys.classify_console_keys`. Its wrapper tightens the wait deadline
without editing that shared module or the C packet.

A successful matching scan produces a protected observed known-host file.
An SSH command uses the independently sealed SSH role, `ssh_transport:true`,
and literal substitutions `@RUN8_KNOWN_HOSTS@` and `@RUN8_SSH_HOST@` resolved only
from HOST_BOUND. Required options are `-F /dev/null`, strict host checking,
batch mode, disabled connection sharing and that exact known-host file; duplicate
options, weakened checking, a changed file or unresolved substitution refuse.
Indirect SSH consumers must use this same checked transport contract and be
included in final source/consumer review. The receipt states that strict checking
is required; it does not claim a future SSH invocation already occurred.

### Persistent literal recording and installation observer

The literal recorder uses `protocol:"persistent-literals-v2"`, a protected
`cwd`, and a nonempty ordered `literals` array. Each entry has a unique `name`,
bound `file` in the full executable closure, `public_contract`, and
`expected_facts`. Scalar public contracts allow boolean, bounded integer and
explicit enum values. Arbitrary captured strings have no export contract.
Writes and reads share one deadline. Failure stops the owned process group and
closes its pipes. Original literals run as standalone Bash statements, without
conditional wrappers that mask errexit. The same Bash retains state across
literals; exclusive numbered observations hold fresh ACK and post-effect facts.

For INSTALLED output, the spec additionally has `native_observer` with role
`install_observer`, source-bound `argv`, and a timeout at most 60 seconds. It runs
after the literals and consumes `RUN8_LITERAL_PREFIX` and its independent SHA.
Its stdout goes through the native output contract above. Scalar recorder
observations alone cannot create an installation event. The workflow then runs
the original signed BF doctor with receipt-selected managed secrets directory,
legitimate signed python3 symlink and exact target/script/module bytes. All
future doctor checks compare the original observed appdb identity and port.

## Restoration and lifecycle

The restoration template contains seven full existing container IDs, eight
starts, four health barriers and three scoped stops. It never recreates a
container, replays a finite job or selects all/latest/ignored failures. Its
Podman executable must equal the independently sealed role. ARMED validates
filled bytes, Console dependency/drop-in, unit properties and their typed raw
facts, owner, linger, initial boot and original identities/effects. Recovery
requires a different boot, automatic success, the same identities/CM1/credential
metadata/doctor, denials and Console effects with matching typed raw evidence.

Observation events and every prefix use exclusive, durable files and a writer
lock; gaps, orphan partial writes, stale context, changed predecessors, replay
and transitions after FAILURE/RETIRED refuse. Extraction/load/install checks
require the current phase, not merely an earlier event. A partial observation is
retained for review and is never silently overwritten or repaired.

## Review boundary

The author supplies source, original RED and final local outputs, mutation
controls, exact test-input/oracle diffs and a whole-file packet index. Mike's
original controls remain untouched; rebound copies preserve every verdict
statement and disclose their input adapters. The one older local deadline
expectation that allowed an attempt at 600 seconds was strengthened to exclude
it, with its exact diff retained. This package remains pending Vera review and
final source/endpoint binding. Nothing here grants host, spend, key, signing,
canonical-copy, source-release, merge-to-main or native Gate authority.
