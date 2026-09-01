# Migration — Apple Container → rootless Podman on macOS

**What it answers:** *"How do we move a running macOS Cortex stack from Apple Container
to rootless Podman without losing data — and what did the first real migration teach us?"*

**Status:** performed once, in production, 2026-09-01 (kaidera-os local Cortex,
`amadmalik@MacBook-Air.local`). Full data fidelity verified: 10 projects / 1,133 agents /
147,397 decisions / 9,558 handoffs / 1,779,465 messages — an exact match against the
pre-migration baseline — plus 22 GiB of graphs and 418 MiB of models seeded into Podman
named volumes. All six services healthy; `/health` OK; API serving migrated data
immediately after cutover. Source: Kai handoffs `5d1c168a` and `d8bce0ba`.

## When to use this path

- The macOS host runs Cortex under **Apple Container** and should move to **rootless
  Podman** (e.g. to converge with the Linux engine strategy).
- The kaidera-os installer now supports this: `KAIDERA_CONTAINER_ENGINE=podman` is
  honoured on Darwin (auto-selection is unchanged — it still resolves to Docker on
  macOS). Two repo patches landed from this migration: the engine gate
  (`scripts/runtime/container-engine.sh`) and the systemd guard
  (`install.sh install_podman_boot_service` early-returns off Linux).

Validated environment: **Podman 6.0.2** rootless (crun/overlay) on an `applehv` machine
with **6 CPU / 10 GiB / 100 GiB**. Treat Podman ≥ 5.0 as the floor, same as Linux.

## The migration sequence

Each step gates the next. The order below is the order that worked; several steps exist
because skipping them was measured to fail.

1. **Baseline the source.** Record exact row counts (`projects`, `agents`, `decisions`,
   `handoffs`, `messages`, `knowledge`), FK constraint count, and installed extensions
   (`vector`, `pg_trgm`, `pgcrypto`). This baseline is the only honest "did it work".
2. **Back up while live.** Apple Container cannot attach a named volume read-only to a
   helper container while it is mounted RW (`VZErrorDomain Code=2`), so stream backups
   out of the running containers:
   `container exec <db> tar czf - -C /var/lib/postgresql/data . > out.tar.gz`, plus
   `pg_dump -Fc` for both databases (`platform_agent_memory`, `harness_app`).
3. **Bootout the old engine's reconciler BEFORE touching ports.** The launchd agent
   `ai.kaidera.kaidera-os.apple-container` reconciles on a 60 s interval and *will*
   resurrect `cortex-pg` mid-migration, re-grabbing published ports. `launchctl bootout`
   it first. Also run `container system stop` — `container stop` alone leaves the Apple
   port forwarders bound on `127.0.0.1:5499/5500/8501`.
4. **Create the Podman machine explicitly.** `podman machine init --provider applehv
   --cpus 6 --memory 10240 --disk-size 100 <name>`. Then **set the default connection
   before starting**: `podman system connection default <name>` — `podman machine start`
   blocks forever on an interactive "make this the default?" prompt when the machine is
   not the default, which hangs automation silently.
5. **Seed the named volumes.** Under Apple Container, graphs/models/vendor were *host
   bind mounts* (`~/Library/Application Support/Kaidera OS/state/apple-container/shared/`);
   Podman named volumes start empty. Seed them:
   `podman run --rm -v <vol>:/dst -v <hostdir>:/src:ro postgres:17-alpine sh -c 'cp -a /src/. /dst/'`.
   The Podman VM sees `/Users` via virtiofs (22 GiB copied in ~60 s).
6. **Bring the stack up with the compose file unchanged**, exporting
   `HOST_PROJECTS_ROOT` and `CLAUDE_STATE_ROOT` — podman-compose reads `.env` from the
   compose directory, not from `local-cortex/.env`, and the compose defaults
   (`/workspace/projects`) do not exist on macOS.
7. **Restore the databases** — see the restore-fidelity section below; this is where the
   migration almost went wrong.
8. **Verify** — see the checklist below.
9. **Boot persistence.** systemd is Linux-only, so the migration hand-created a launchd
   agent (`ai.kaidera.kaidera-os.podman.plist`, `RunAtLoad` + `StartInterval 300`) that
   execs a wrapper ensuring `podman machine start <name>` before the installer-generated
   `run-cortex-podman.sh up`. The periodic up-list must **exclude one-shot migrate
   services** (e.g. `harness-appdb-migrate`) — podman-compose flakes when re-running
   them every interval.
10. **After a soak period**, reclaim the Apple Container store
    (`~/Library/Application Support/Kaidera OS/state/apple-container/shared/`, ~22 GiB in
    the reference migration) and remove the Apple runtime if nothing else needs it.

## The restore-fidelity discovery (read this before any pg_restore)

**`pg_restore --no-owner --no-privileges` silently breaks the `cortex_app` role.**

In the source database, 36 public tables (including `handoffs` and `agents`) were
*owned* by `cortex_app`, with access granted by ACL. Restoring with `--no-owner`
reassigned everything to `postgres`; `--no-privileges` dropped the ACLs. The API's app
role lost all access and the writer gate failed closed with a misleading
`503 'roster policy unavailable'` — the root cause (`InsufficientPrivilegeError:
permission denied for table handoffs`) was only visible in `podman logs cortex-api`.

Rules that are now permanent:

- **Never use `--no-owner` / `--no-privileges` when restoring Cortex databases.** If
  role errors must be avoided, immediately replay ownership and ACLs afterwards as two
  separate passes: extract `OWNER TO` statements and `GRANT`/`REVOKE` (ACL) sections
  from the dump (`pg_restore -l` / `--section` or `pg_dump --schema-only` filtered) and
  apply them against the restored database.
- The `harness_app` database needed the same treatment (21 `OWNER TO harness`
  statements; its dump contained zero ACL entries — owner-based access only).
- **Diagnosis rule:** the writer gate 503 is fail-closed on *any* roster-load exception.
  Always read the API container logs for the real error before theorising.

## pg_restore gotchas (all measured during this migration)

- **`DROP SCHEMA public CASCADE` is not enough.** The installer pre-migrates schema
  `cortex`; restoring into that database without also dropping schema `cortex` produces
  ~139 benign "already exists" collisions. Drop **both** schemas, or restore into a
  freshly created database.
- **Fidelity was otherwise byte-exact** with plain `pg_dump -Fc` / `pg_restore`: exact
  row counts, 76/76 FK constraints, all extensions (`vector`, `pg_trgm`, `pgcrypto`)
  present.
- **The writer-gate probe is the real smoke test.** After restore, `GET /handoffs` *as
  the app role* must return data, and `POST /handoffs` must succeed — both migration
  handoffs were written through the gate as the final proof.

## Podman-on-macOS operational notes

- **One VM at a time.** Podman on macOS runs a single machine; starting a second errors
  "only one VM can be active at a time".
- **Never-booted machines can fail Ignition** ("only Ignition spec v3.0.0+ configs are
  accepted"), leaving the VM in emergency mode. Fix: `podman machine rm` (zero data —
  named volumes live in the machine but a never-started machine has none) and re-init
  explicitly.
- **Default `machine init` is wrong for this stack on some hosts**: libkrun provider and
  2 GiB RAM hung silently on first boot. `applehv` + ≥ 10 GiB is the proven
  configuration.
- **"internal libpod error" masks port conflicts.** If a container fails to start with
  that opaque message, re-run with `podman --log-level=debug start <container>` to see
  the real bind error — usually a stale Apple Container port forwarder still holding
  `5499/5500/8501`.
- **podman-compose 1.5.0 is fragile on re-up**: `up` deletes-then-recreates (a failed
  recreate leaves the container simply *gone*), one-shot services flake, and stale
  dependent pairs block recreation. Recovery: `podman compose down --remove-orphans`
  then `up -d` — named volumes survive.
- **`/healthz` does not exist** on this Cortex version; `/health` is the real endpoint.
  Runbooks referencing `/healthz` are wrong.

## Verification checklist

1. `podman ps` — `cortex-pg`, `harness-appdb`, `cortex-api`, `cortex-cli`,
   `cortex-graph-worker`, `cortex-pdf-worker` all Up/healthy.
2. `GET /health` — healthy, postgres connected, expected schema version.
3. Row counts exactly match the pre-migration baseline (step 1).
4. `information_schema.table_privileges` + `pg_tables` ownership match source
   (`cortex_app` owns its 36 tables; `harness` owns its 21).
5. `GET /projects` via admin token returns every project.
6. Writer-gate probe: `GET /handoffs` and `POST /handoffs` as the app role both succeed.
7. `graph-worker` sees the seeded graphs at `/var/lib/cortex/graphs`.
8. The launchd podman agent reconciles cleanly (exit 0).

## Rollback path

- Backups are the rollback: retain the `pg_dump` archives, app-DB dump, and volume tars
  from step 2 (the reference migration kept them under `/tmp/cortex-backup-<stamp>/`).
- To return to Apple Container: bootout the podman launchd agent, `podman compose down`
  (named volumes survive; remove the machine only if abandoning Podman entirely),
  re-load `ai.kaidera.kaidera-os.apple-container`, and bring the Apple stack up from its
  retained volumes — or restore from the dumps if the volumes were touched.
- Docker/runc-equivalents are not involved on macOS; nothing needs reinstalling to roll
  back, which is why step 3's bootout (not uninstall) is the correct disabling act.

## Follow-ups still open

- Generalise the launchd boot-persistence wrapper into `install.sh` (today it is
  hand-created per host).
- Extend `scripts/runtime/migrate-docker-to-podman.sh` (Linux-only) into an
  engine-migration runner that also covers Apple Container → Podman on macOS, with the
  ownership/ACL replay folded in as a standard post-restore step.
- Installer preflights: old-engine reconciler detection/bootout, stale published-port
  holders on `5499/5500/8501/8765`, `podman system connection default` matching the
  target machine, and machine provider/memory (applehv, ≥ 10 GiB).
- SPA build preflight: `npm ci` lockfile-sync check with a clear error before
  `build-spa.sh` (the `@emnapi/wasi-threads` lockfile drift killed the install mid-run).
- `design/17-container-engine-strategy.md` still names Apple Container as the macOS
  default; with Podman viable on macOS the strategy doc needs a decision update.
