# ship/run contract (option A, .004 day 2)

Owner bob@helix. Accepted plan: DAY-1 + INDEX v2 (J03, J06, J07, J08, J11), extended by DAY-2 (H-D622).
Joined to Core `next/src/cortex_core/runtime/contract.md` (cox, composed at 090698743d9c) and cox's
ruling `review/cortex/design/J06-process-transfer-v1.md` (Q1-Q4). A change to a frozen item below is
a consult, not a silent edit.

## Command

    python3 -I render.py --config FILE --operations FILE --release FILE --core-conf DIR --out DIR

Host stdlib Python plus the release's own Core source (`next/src`); no other package.

| flag | what | owner |
|---|---|---|
| `--config` | the ONE vault `config/cortex.json`, Core's closed format | cox format, nix writes |
| `--operations` | `next/contracts/runtime/runtime-operations-v1.json`, Core's RC0 pin | contract |
| `--release` | verified release target: `source_commit`, `core.{image,initdb,postgres,psql}`, `api.image` | nix |
| `--core-conf` | rendered `postgresql.conf`, `pg_hba.conf`, `sql/roles-{pre,post,close}.sql` | nix templates |
| `--out` | empty or absent output folder | installer |

## One parser

The config is read only by Core: `read_private_file` (private, owned, no symlink, no hard link)
and `decode_config(raw, mode=m)` for the three mode views `serve`, `migrate`, `seed`. render hands
Core the caller's `--config` spelling unresolved, so Core's no-follow walk sees every component: a
relative path, `..`, or a symlinked file or parent folder refuses `private_file_required`. The plan
mounts that same validated path. Each view admits only Core's `MODE_PURPOSES[m]`. render adds no
config parser, default or field of its own; every config refusal is Core's own reason. The
operations and release documents use Core's `decode`, and the operations pin is Core's `RC0_SHA256`.

## Core containers and custody (J06)

Every Core container runs the actual `/opt/cortex/python/bin/python3 -I -B -m
cortex_core.runtime.operator <verb> --config /run/cortex/config/cortex.json --unlock-fd 3 --key-id
<input>`; `status` takes `--config` only. `--preserve-fds=1` hands it the runner's one unlock
descriptor as fd 3. The encrypted vault is the only credential store: Core decrypts in memory; no
plaintext projection file or podman secret reaches a Core container. Core's walk needs `vault_root`,
`secrets` and `secrets/cortex` owned and 0700, so each is a private tmpfs (`rw,mode=0700,size=64k`)
and each encrypted record is bind-mounted read-only at its own Core path (host path = Core path).

| step | verb | records mounted | extra |
|---|---|---|---|
| `migrate` | `migrate` | migrator, root cert (ro) | config |
| `seed` | `seed --operation-key <input>` | its one record directory `secrets/cortex`, writable (atomic replace + lock) | config |
| `serve`, `api` | `serve` | api_request, probe, root cert (ro) | config, `/tmp` |
| `serve`, `core` | the contract's `core-server` vector | none | data volume, conf files |

Seed's writable directory also holds the other encrypted records; Core's mode-bound resolve refuses
them before file access (ruling Q3). Serve never gets that directory.

## Outputs (written only when every check passes)

- `bootstrap.plan.json`: `inputs` = `unlock_descriptor`, `key_id`, `seed_operation_key` (explicit
  at run time, never rendered or defaulted; argv holds `{"input": "key_id"}` and
  `{"input": "seed_operation_key"}` markers). Nine steps in fixed order, each with a contract budget
  deadline and zero retries: `core-init`, `temporary-db`, `roles-pre`, `migrate`, `roles-post`,
  `seed`, `roles-close`, `stop-temporary-db`, `serve`. nix's `install_main` runs it (J03).
- The `serve` step is DB + API only: `podman pod create` (one `--publish 127.0.0.1:<publish>:<bind>`),
  then `podman run -d --pod cortex-v2 --restart=no` for `core` and for `api`. It is also the explicit
  restart: an unattended restart cannot resupply the unlock descriptor, so nothing claims one.
- `inventory.json`: containers, images, serve purposes, the `ready` hook
  (`http://127.0.0.1:<publish.port>/health`, fields `component,status,readiness`, source commit)
  and `status_argv`.

## Typed refusals

Exit 2, stderr one JSON line `{"reason": ..., "unit": "ship/run"}`, nothing written. Core's own
reasons pass through unchanged (for example `config_shape`, `unshipped_provider_refusal`,
`config_transport_unratified`, `config_binding`, `private_file_required`, `raw_shape`,
`operations_mismatch`). render's own: `input_missing:<name>`, `bad_argument:<flag>`,
`vector_field_unknown:<field>`, `out_not_empty`.

## Open joins

- cox: Core's operator `serve` and `status` are still unbound (`operator_unbound`); the serve step
  is rendered against the actual argument grammar and refuses until cox binds them.
- finn/nix: seed authority and the seed operation key's source.
- nix: the installer SQL steps (`core-init`, `roles-*`) still take the bootstrap login and role
  secrets as Day-1 podman secrets; their source under encrypted custody (role import) is nix's
  roles/install join, not changed here. The runner that supplies the three inputs, TLS server
  material (J08 verify-full) and the service-manager restart are nix's.
- day 2, real podman: `--preserve-fds` into a detached pod container, tmpfs parent ownership and UID
  mapping as Core's walk requires, and the bootstrap run on both hosts. These render tests use
  source fixtures and are not install proof.
