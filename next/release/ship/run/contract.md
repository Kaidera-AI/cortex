# ship/run contract (option A, .004 day 1)

Owner bob@helix. Accepted plan: DAY-1 + INDEX v2 (J03, J06, J07, J08, J11). This file is the
interface receipt; a change by cox, nix or finn to a frozen item below is a consult, not a silent edit.

## Command

    python3 -I render.py --config FILE --operations FILE --release FILE --core-conf DIR --out DIR

stdlib only. Every input is explicit; a missing one is a typed GAP, never a default.

| flag | what | owner |
|---|---|---|
| `--config` | the ONE `config/cortex.json` from the project vault (cox's format) | cox format, nix writes |
| `--operations` | `next/contracts/runtime/runtime-operations-v1.json`, sha256-pinned in `render.py` | contract |
| `--release` | verified release target descriptor: `source_commit`, `core.{image,initdb,postgres,psql}`, `api.image` | nix |
| `--core-conf` | rendered `postgresql.conf`, `pg_hba.conf`, `sql/roles-{pre,post,close}.sql` | nix templates |
| `--out` | empty or absent output folder | installer |

## Config fields read (frozen field map; all other fields belong to cox's parser)

| field | use |
|---|---|
| `installation_id` | label `ai.kaidera.cortex.installation` |
| `ai_mode` | must be `off` (J05); missing refuses |
| `bind.port` | API container port inside the pod |
| `publish.host`, `publish.port` | the ONE hostPort; host must be `127.0.0.1` |
| `database.tls.mode` | must stay `verify-full` until the CTO transport ruling (J08) |
| `purposes.<p>.secret_ref` | presence of `api_request`, `probe`, `bootstrap`, `migrator`, `installer_enrollment` |

Projection: purpose `p` is the podman secret `cortex-v2-<p>` (a disposable copy of the vault
custody), mounted read-only at `/run/cortex/secrets/<p>`. The config file is mounted read-only at
`/run/cortex/config/cortex.json`. No vault folder and no secrets folder is ever mounted.

## Frozen argv

- Cortex processes: `/opt/cortex/python/bin/python3 -I -B -m cortex_core.cli <serve|migrate|seed|status> --config /run/cortex/config/cortex.json`
  (cox: `cortex {serve,migrate,seed,status} --config FILE`). The stale `api`, `supervisor`,
  `migrate` and `status` vectors in the operations contract are NOT used (J07); nothing uses 9309.
- Database: the contract's `core-init` and `core-server` vectors, fields resolved from `--release`
  and `--config`, bytes otherwise unchanged.

## Outputs (written only when every check passes)

- `cortex-v2.pod.yaml`: serve Pod, JSON text (valid YAML 1.2), exactly two containers, `core` and
  `api`. Only `api` gets secrets: `api_request` and `probe`. One hostPort on `127.0.0.1`.
- `bootstrap.plan.json`: the finite bootstrap, nine steps in this fixed order, each with a deadline
  and only its own secrets: `core-init`, `temporary-db`, `roles-pre`, `migrate`, `roles-post`,
  `seed`, `roles-close`, `stop-temporary-db`, `serve`. No step repeats or retries. nix's
  `install_main` runs it and owns the install record (J03).
- `inventory.json`: containers, images and secrets per unit, plus the `ready` hook
  (`http://127.0.0.1:<publish.port>/health`, fields `component,status,readiness`, the source commit).

## Typed refusals

Exit 2, stderr one JSON line `{"reason": ..., "unit": "ship/run"}`, nothing written:
`input_missing:<name>`, `operations_mismatch`, `ai_mode_missing`, `ai_mode_not_off:<v>`,
`transport_unratified:<mode>`, `unsafe_publish:<host>`, `purpose_missing:<p>`, `out_not_empty`.

## Open joins

- cox: confirm the projection root `/run/cortex/secrets/<purpose>` for the resolver, and the
  `serve/migrate/seed/status --config` argv above.
- nix: TLS material for verify-full (make_tls, J08) is not projected here until ratified.
- nix: `core-init` runs only when `PG_VERSION` is absent (`when` field in the plan).
