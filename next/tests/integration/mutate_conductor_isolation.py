"""X02 literal semantic recipes; bound runner/classifier remain shared."""

from datetime import datetime, timezone
import json
import re
import shutil
import subprocess
import time
import unittest

import mutate_conductor as proof

FIXTURE = proof.ROOT / "tests/integration/x02_process_fixture.py"
GATEWAY = proof.ROOT / "src/cortex_core/gateway/health_metrics.py"
PREFIX = "test_conductor_isolation.ConductorIsolationTests."
GUARDED = '''    async def guarded(self, action):
        """PG-only control-intent callback. External side effects are forbidden here."""
        token = self._token()
        async with self._transaction() as conn:
            valid = await conn.fetchval(
                """SELECT fence FROM coordination.supervisor_leases
                WHERE installation_id=$1 AND holder=$2 AND fence=$3
                  AND expires_at > clock_timestamp() FOR UPDATE""",
                *token,
            )
            if valid is None:
                raise StaleLease("Stale supervisor cannot issue control intent")
            remaining = await conn.fetchval(
                """SELECT EXTRACT(EPOCH FROM (expires_at-clock_timestamp()))
                FROM coordination.supervisor_leases WHERE installation_id=$1 AND holder=$2 AND fence=$3""",
                *token,
            )
            try:
                result = await asyncio.wait_for(
                    action(conn, token[2]), timeout=max(0, float(remaining))
                )
            except TimeoutError:
                raise StaleLease(
                    "Control action outlived its lease; transaction rolled back"
                ) from None
            live = await conn.fetchval(
                """SELECT expires_at > clock_timestamp()
                FROM coordination.supervisor_leases WHERE installation_id=$1 AND holder=$2 AND fence=$3""",
                *token,
            )
            if not live:
                raise StaleLease(
                    "Control action outlived its lease; transaction rolled back"
                )
            return result
'''
ACTION_BOUND = GUARDED[GUARDED.index("            try:\n"):GUARDED.index("            return result\n")]
MUTATIONS = [
    (FIXTURE, "external_restart_disabled", "if restarts >= self.max_restarts:", "if True:",
     PREFIX + "test_external_restart_uses_new_pid_and_fence"),
    (proof.SUPERVISOR, "live_overlap_admitted",
     "WHERE coordination.supervisor_leases.expires_at <= clock_timestamp()", "WHERE TRUE",
     PREFIX + "test_live_overlap_is_refused_without_affecting_core"),
    (proof.SUPERVISOR, "reclaim_reuses_fence", "fence=coordination.supervisor_leases.fence+1",
     "fence=coordination.supervisor_leases.fence",
     PREFIX + "test_external_restart_uses_new_pid_and_fence"),
    (proof.SUPERVISOR, "stale_control_token_admitted", GUARDED,
     GUARDED.replace("AND holder=$2 AND fence=$3",
                     "AND ($2::uuid IS NULL OR TRUE) AND ($3::bigint IS NULL OR TRUE)"),
     PREFIX + "test_stale_control_is_refused_after_takeover"),
    (proof.SUPERVISOR, "expired_control_committed", GUARDED,
     GUARDED.replace(ACTION_BOUND, "            result = await action(conn, token[2])\n"),
     PREFIX + "test_expiry_receipt_proves_callback_entered_before_rollback"),
    (FIXTURE, "independent_consumer_disabled", "    async def advance(self):\n",
     "    async def advance(self):\n        return 0  # Simulate a control-dependent consumer refusal.\n",
     PREFIX + "test_consumer_continues_without_conductor"),
    (GATEWAY, "dead_conductor_reported_healthy", '"degraded" if available else "unavailable"',
     '"ok" if available else "unavailable"', PREFIX + "test_external_health_survives_dead_child"),
    (FIXTURE, "restart_cap_exceeded", "if restarts >= self.max_restarts:",
     "if restarts >= self.max_restarts + 1:",
     PREFIX + "test_restart_exhaustion_is_explicit_and_bounded"),
    (FIXTURE, "consumer_checkpoint_not_advanced",
     'await conn.execute("UPDATE public.x02_checkpoint SET seq=$1 WHERE id=1", rows[-1]["seq"])',
     "pass", PREFIX + "test_consumer_continues_without_conductor"),
    (FIXTURE, "background_fixture_failure_hidden", "if failures and not self.reported_failure:",
     "if False:", PREFIX + "test_background_fixture_failure_surfaces_after_cleanup"),
    (FIXTURE, "replacement_stop_ignored",
     '            # Spawn publishes only after readiness; stop may arrive during that await.\n            if self.closing or self.requested_stop:',
     '            # Spawn publishes only after readiness; stop may arrive during that await.\n            if False:',
     "test_conductor_isolation.FixtureLifecycle.test_stop_during_replacement_spawn_closes_replacement"),
    (FIXTURE, "runtime_close_error_hidden",
     '        if not self.reported_failure:\n            self.reported_failure = True\n            self.check_exit()',
     '        if not self.reported_failure:\n            self.reported_failure = True',
     "test_conductor_isolation.FixtureLifecycle.test_fixture_error_exit_cannot_be_silent_successful_close"),
    (FIXTURE, "runtime_watcher_error_restarted",
     '            self.child.check_exit()', '            pass',
     "test_conductor_isolation.FixtureLifecycle.test_watcher_refuses_runtime_fixture_error_before_restart"),
    (FIXTURE, "late_heartbeat_error_hidden",
     '                await heartbeat  # Late runtime failures must survive successful commands.',
     '                await asyncio.gather(heartbeat, return_exceptions=True)',
     "test_conductor_isolation.FixtureLifecycle.test_late_heartbeat_failure_survives_pool_cleanup"),
    (proof.ROOT / "tests/integration/test_conductor_isolation.py", "composed_cleanup_short_circuited",
     '            except BaseException as error:\n                errors.append(error)\n        try:\n            await self.fixture.asyncTearDown()',
     '            except BaseException as error:\n                raise\n        try:\n            await self.fixture.asyncTearDown()',
     "test_conductor_isolation.FixtureLifecycle.test_teardown_attempts_all_owned_cleanup_after_one_failure"),
    (proof.ROOT / "tests/integration/test_conductor_isolation.py", "partial_setup_cleanup_unregistered",
     '        self.addAsyncCleanup(self.asyncTearDown)', '        pass',
     "test_conductor_isolation.FixtureLifecycle.test_partial_setup_has_registered_owned_cleanup"),
    (proof.ROOT / "tests/integration/test_conductor_isolation.py", "remaining_pg_pools_skipped",
     '            for name in ("control_pool", "pool", "admin"):', '            for name in ():',
     "test_conductor_isolation.FixtureLifecycle.test_failed_fixture_teardown_still_closes_remaining_pools"),
]


def validate_recipes():
    """Refuse syntax/selector drift before any stack or temporary source edit."""
    names = set()
    for path, name, before, after, target in MUTATIONS:
        if name in names or before == after or path.read_text().count(before) != 1:
            raise RuntimeError("INCONCLUSIVE: recipe drift: " + name)
        names.add(name)
        if not re.fullmatch(r"test_[a-z_]+\.[A-Za-z]+\.test_[a-z_]+", target):
            raise RuntimeError("INCONCLUSIVE: invalid test selector: " + name)
        loader = unittest.TestLoader()
        suite = loader.loadTestsFromName(target)
        cases = []

        def flatten(node):
            if isinstance(node, unittest.TestSuite):
                for child in node:
                    flatten(child)
            else:
                cases.append(node)

        flatten(suite)
        if loader.errors or len(cases) != 1 or cases[0].id() != target:
            raise RuntimeError("INCONCLUSIVE: selector did not load exactly one test: " + name)
        compile(path.read_text().replace(before, after), str(path), "exec")


def resource_gate(evidence, name):
    podman = shutil.which("podman")
    if not podman:
        raise RuntimeError("INCONCLUSIVE: disposable runner unavailable")
    attempts = []
    while True:
        memory = subprocess.check_output(["memory_pressure"], text=True)
        match = re.search(r"System-wide memory free percentage:\s*(\d+)%", memory)
        if not match:
            raise RuntimeError("INCONCLUSIVE: memory gate observation unavailable")
        inventory = subprocess.check_output(
            [podman, "ps", "-a", "--filter", "label=cortex.test", "--format", "{{.Names}}"], text=True)
        free = int(match[1])
        allowed = free >= 35 and not inventory.strip()
        attempts.append({"utc": datetime.now(timezone.utc).isoformat(), "free_percent": free,
                         "team_test_inventory": inventory, "allowed": allowed})
        (evidence / (name + ".resources.json")).write_text(json.dumps(attempts, indent=2) + "\n")
        if allowed:
            return podman
        print(f"resource gate waiting: free={free}%, team slot occupied={bool(inventory.strip())}", flush=True)
        time.sleep(30)


EXECUTE = proof.execute


def execute(evidence, name, target=None):
    podman = resource_gate(evidence, name)
    code, output = EXECUTE(evidence, name, target)
    inventory = subprocess.check_output(
        [podman, "ps", "-a", "--filter", "label=cortex.worker=nemo", "--filter",
         "label=cortex.test", "--format", "{{.Names}}"], text=True)
    matches = re.findall(r"^cleanup: (PASS|FAIL)$", output, re.M)
    receipt = {"nemo_test_inventory": inventory, "last_cleanup": matches[-1] if matches else None}
    (evidence / (name + ".cleanup.json")).write_text(json.dumps(receipt, indent=2) + "\n")
    if inventory.strip() or not matches or matches[-1] != "PASS":
        raise RuntimeError("INCONCLUSIVE: disposable cleanup failed")
    return code, output


def run():
    validate_recipes()
    proof.MUTATIONS = MUTATIONS
    proof.execute = execute
    proof.run()


if __name__ == "__main__":
    run()
