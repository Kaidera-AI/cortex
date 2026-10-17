# ship/run contract (option A, .004 day 1)

Owner bob@helix. Accepted plan: DAY-1 + INDEX v2 (J03, J06, J07, J08, J11), extended by DAY-2 (H-D622).
Joined to the published Core interface `next/src/cortex_core/runtime/contract.md` (cox, composed at
8e1babb2). A change to a frozen item below is a consult, not a silent edit.

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
relative path, `..`, or a symlinked file or parent folder refuses `private_file_required`. The pod
and plan mount that same validated path. Each view
admits only Core's `MODE_PURPOSES[m]`. render adds no config parser, default or field of its own;
every config refusal is Core's own reason. The operations and release documents use Core's
`decode`, and the operations pin is Core's `RC0_SHA256`.

## Projections (disposable copies of the vault custody)

For a Core ref `secrets/cortex/<name>`, the podman secret `cortex-v2-<name>` is mounted read-only
as one file at `<vault_root>/secrets/cortex/<name>`, which is exactly where Core's resolver reads.
Only the selected per-purpose refs of each mode view are projected, plus `database.tls.root_cert_ref`
wherever the database is reached. No vault folder and no secrets folder is ever mounted. Each
projection's Pod volume is `proj-<first 16 hex of sha256(ref)>`, a DNS label taken from the whole
case-sensitive ref; a duplicate Pod volume name refuses `projection_collision` before anything is
written. The config
file is mounted read-only at `/run/cortex/config/cortex.json` (proposed day-2 seam).

| unit | Core purposes | plus |
|---|---|---|
| serve pod, `api` | `serve`: api_request, probe | root cert |
| serve pod, `core` | none | none |
| `migrate` step | `migrate`: migrator | root cert, config |
| `seed` step | `seed`: bootstrap, installer_enrollment, api_request | root cert, config |
| `core-init` | the bootstrap ref, at the vector's fixed `/run/cortex/bootstrap/initdb-password` | none |
| nix SQL steps | bootstrap login (`PGPASSWORD`, in-container env); `roles-pre` also api_request, installer_enrollment, migrator, probe | root cert, `PGSSLMODE=verify-full` |

## Frozen argv

- Cortex processes: `/opt/cortex/python/bin/python3 -I -B -m cortex_core.cli <serve|migrate|seed|status> --config /run/cortex/config/cortex.json`
  (proposed day-2 seam: Core's operator dispatch is day 2). The stale `api`, `supervisor`,
  `migrate` and `status` vectors are NOT used (J07); nothing uses 9309.
- Database: the contract's `core-init` and `core-server` vectors; fields resolved from `--release`
  and the bootstrap login; bytes otherwise unchanged.

## Outputs (written only when every check passes)

- `cortex-v2.pod.yaml`: serve Pod, JSON text (valid YAML 1.2), exactly two containers, `core` and
  `api`. One hostPort on `127.0.0.1` (Core admits only a loopback publish host).
- `bootstrap.plan.json`: nine steps in fixed order, each with its own projections, a contract
  budget deadline and zero retries: `core-init`, `temporary-db`, `roles-pre`, `migrate`,
  `roles-post`, `seed`, `roles-close`, `stop-temporary-db`, `serve`. nix's `install_main` runs it (J03).
- `inventory.json`: containers, images, serve purposes and the `ready` hook
  (`http://127.0.0.1:<publish.port>/health`, fields `component,status,readiness`, source commit).

## Typed refusals

Exit 2, stderr one JSON line `{"reason": ..., "unit": "ship/run"}`, nothing written. Core's own
reasons pass through unchanged (for example `config_shape`, `unshipped_provider_refusal`,
`config_transport_unratified`, `config_binding`, `private_file_required`, `raw_shape`,
`operations_mismatch`). render's own: `input_missing:<name>`, `bad_argument:<flag>`,
`vector_field_unknown:<field>`, `projection_collision`, `out_not_empty`.

## Open joins

- Core: the composition base is cox's 8e1babb2 (Hale delta review CONFIRMED); Core's Day-2
  command and custody pins are composed only when cox publishes them.
- nix: make_secret creates `cortex-v2-<name>` from each vault ref; TLS server material for
  verify-full (J08) stays with nix; role-step login and SQL are nix's.
- day 2, real podman: `kube play` of the JSON-text pod, the `subPath` single-file secret
  projection, file ownership as Core's resolver requires, and the bootstrap run on both hosts.
  These render tests use source fixtures and are not install proof.
- J06 custody encryption/layout/unlock is the CTO's next-slice decision; nothing here stores secrets.
