"""Lost-create acknowledgements: observable cleanup and collision preservation."""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import unittest
from vector_baseline import postgres


def create_resource(args):
    if args[:2] == ["volume", "create"]:
        return "volume", args[-1]
    if args[:2] == ["secret", "create"]:
        return "secret", args[-2]
    if args[0] == "create":
        return "container", args[args.index("--name") + 1]
    return None


class ResourceEngine:
    """Stateful external effects, independent of the harness's ownership ledger."""
    def __init__(self, *, fault=None, error=RuntimeError, side_effect=True):
        self.resources = {kind: {} for kind in ("container", "volume", "secret")}
        self.fault, self.error, self.side_effect = fault, error, side_effect
        self.commands, self.created, self.removed = [], [], []
        self.remove_failures, self.inspect_failures = {}, {}
        self.bad_inspection = False

    def seed(self, kind, name, labels=None):
        self.resources[kind][name] = {"labels": labels or {}, "id": kind + "-id-" + name}

    def fail(self):
        if self.error is subprocess.TimeoutExpired:
            raise subprocess.TimeoutExpired(["podman", "create"], 60)
        raise self.error("fixture acknowledgement error")

    def __call__(self, args, **kwargs):
        # Store the presence of stdin, never its credential value.
        self.commands.append((list(args), kwargs.get("input") is not None))
        creation = create_resource(args)
        if creation:
            kind, name = creation
            if name in self.resources[kind]:
                raise RuntimeError("fixture name collision")
            if kind == self.fault and not self.side_effect:
                self.fail()
            labels = {}
            for i, arg in enumerate(args):
                if arg == "--label":
                    key, value = args[i + 1].split("=", 1)
                    labels[key] = value
            self.seed(kind, name, labels)
            self.created.append((kind, name))
            if kind == self.fault:
                self.fail()
            return self.resources[kind][name]["id"]
        if args[0] == "start":
            raise RuntimeError("fixture stop after acknowledged creates")
        if args[0] == "ps":
            return "\n".join(self.resources["container"])
        if args[:2] in (["volume", "ls"], ["secret", "ls"]):
            return "\n".join(self.resources[args[0]])
        if len(args) > 1 and args[1] == "inspect":
            kind, name = args[0], args[-1]
            if self.inspect_failures.get(kind, 0):
                self.inspect_failures[kind] -= 1
                raise RuntimeError("fixture inventory acknowledgement error")
            if self.bad_inspection:
                return "[]"
            row = self.resources[kind][name]
            if kind == "container":
                data = {"Id": row["id"], "Name": "/" + name, "Config": {"Labels": row["labels"]}}
            elif kind == "secret":
                data = {"ID": row["id"], "Spec": {"Name": name, "Labels": row["labels"]}}
            else:
                data = {"Name": name, "Labels": row["labels"]}
            return json.dumps([data])
        if args[:2] == ["rm", "-f"] or args[:2] in (["volume", "rm"], ["secret", "rm"]):
            kind = "container" if args[0] == "rm" else args[0]
            if self.remove_failures.get(kind, 0):
                self.remove_failures[kind] -= 1
                raise RuntimeError("fixture removal error")
            target = args[-1]
            for name, row in list(self.resources[kind].items()):
                if target in (name, row["id"]):
                    del self.resources[kind][name]
                    self.removed.append((kind, name))
                    return ""
            raise RuntimeError("fixture removal target absent")
        raise AssertionError("unexpected fixture operation: " + repr(args))


class PendingCleanupTests(unittest.TestCase):
    def assert_released(self, stack):
        self.assertIsNone(stack.password)
        self.assertIsNone(stack.lock)
        lock_path = Path.home() / ".cache/kaidera/b01-podman.lock"
        with lock_path.open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(handle, fcntl.LOCK_UN)

    def exercise(self, kind, error, *, side_effect=True):
        engine = ResourceEngine(fault=kind, error=error, side_effect=side_effect)
        stack = postgres.DisposablePostgres(runner=engine)
        entered, caught = False, None
        try:
            with stack:
                entered = True
        except Exception as exc:
            caught = exc
        self.assertFalse(entered)
        self.assertEqual(engine.resources, {kind: {} for kind in engine.resources},
                         "created effects must be removed even when acknowledgement is lost")
        self.assertEqual(set(engine.created), set(engine.removed))
        self.assertIsInstance(caught, error)
        self.assertTrue(stack.cleanup_verified)
        self.assertEqual(stack.owned, set())
        self.assert_released(stack)

    def test_volume_create_then_timeout_is_removed(self):
        self.exercise("volume", subprocess.TimeoutExpired)

    def test_secret_create_then_timeout_is_removed(self):
        self.exercise("secret", subprocess.TimeoutExpired)

    def test_container_create_then_timeout_is_removed(self):
        self.exercise("container", subprocess.TimeoutExpired)

    def test_volume_create_then_ack_error_is_removed(self):
        self.exercise("volume", RuntimeError)

    def test_secret_create_then_ack_error_is_removed(self):
        self.exercise("secret", RuntimeError)

    def test_container_create_then_ack_error_is_removed(self):
        self.exercise("container", RuntimeError)

    def test_no_side_effect_create_errors_are_safe(self):
        for kind in ("volume", "secret", "container"):
            for error in (RuntimeError, subprocess.TimeoutExpired):
                with self.subTest(kind=kind, error=error.__name__):
                    self.exercise(kind, error, side_effect=False)

    def test_colliding_resources_are_preserved(self):
        for kind in ("volume", "secret", "container"):
            with self.subTest(kind=kind):
                engine = ResourceEngine()
                stack = postgres.DisposablePostgres(runner=engine)
                engine.seed(kind, stack.name, {"kaidera.b01.lifecycle": "another-run"})
                before = dict(engine.resources[kind][stack.name])
                with self.assertRaises(RuntimeError):
                    with stack:
                        self.fail("a collision must refuse startup")
                self.assertEqual(engine.resources[kind][stack.name], before)
                self.assertEqual(set(engine.removed), set(engine.created))
                self.assertTrue(stack.cleanup_verified, "verification concerns this lifecycle only")
                self.assert_released(stack)

    def test_unrelated_resources_and_credentials_are_untouched(self):
        engine = ResourceEngine(fault="container", error=subprocess.TimeoutExpired)
        for kind in engine.resources:
            engine.seed(kind, "unrelated-existing", {"kaidera.b01.lifecycle": "old"})
        stack = postgres.DisposablePostgres(runner=engine)
        with self.assertRaises(subprocess.TimeoutExpired):
            with stack:
                self.fail("lost creation acknowledgement must not enter body")
        self.assertEqual([list(rows) for rows in engine.resources.values()],
                         [["unrelated-existing"]] * 3)
        self.assertEqual(set(engine.created), set(engine.removed))
        self.assert_released(stack)

    def test_removal_error_is_retained_and_retry_removes_every_effect(self):
        for kind in ("volume", "secret", "container"):
            with self.subTest(kind=kind):
                engine = ResourceEngine(fault="container", error=subprocess.TimeoutExpired)
                engine.remove_failures[kind] = 1
                stack = postgres.DisposablePostgres(runner=engine)
                with self.assertRaises(RuntimeError):
                    stack.__enter__()
                self.assert_released(stack)
                self.assertFalse(stack.cleanup_verified)
                self.assertEqual(list(engine.resources[kind]), [stack.name])
                retry = None
                try:
                    stack.close()
                except Exception as exc:
                    retry = exc
                self.assertEqual(engine.resources, {key: {} for key in engine.resources})
                self.assertIsNone(retry)
                self.assertTrue(stack.cleanup_verified)

    def test_pending_inspection_failure_refuses_success_and_is_retryable(self):
        engine = ResourceEngine(fault="volume", error=subprocess.TimeoutExpired)
        engine.inspect_failures["volume"] = 1
        stack = postgres.DisposablePostgres(runner=engine)
        with self.assertRaises(RuntimeError):
            stack.__enter__()
        self.assertFalse(stack.cleanup_verified)
        self.assertEqual(list(engine.resources["volume"]), [stack.name])
        self.assert_released(stack)
        retry = None
        try:
            stack.close()
        except Exception as exc:
            retry = exc
        self.assertEqual(engine.resources, {key: {} for key in engine.resources})
        self.assertIsNone(retry)
        self.assertTrue(stack.cleanup_verified)

    def test_unverified_identity_is_never_removed(self):
        engine = ResourceEngine(fault="volume", error=subprocess.TimeoutExpired)
        engine.bad_inspection = True
        stack = postgres.DisposablePostgres(runner=engine)
        with self.assertRaises(RuntimeError):
            stack.__enter__()
        self.assertEqual(engine.removed, [])
        self.assertFalse(stack.cleanup_verified)
        self.assert_released(stack)
        engine.bad_inspection = False
        retry = None
        try:
            stack.close()
        except Exception as exc:
            retry = exc
        self.assertEqual(engine.resources, {key: {} for key in engine.resources})
        self.assertIsNone(retry)


@unittest.skipUnless(os.environ.get("B01_PODMAN") == "1", "explicit disposable Podman fixture")
class NativeCreateFailureTests(unittest.TestCase):
    def test_actual_created_effects_removed_after_six_lost_acknowledgements(self):
        for kind in ("volume", "secret", "container"):
            for error in (subprocess.TimeoutExpired, RuntimeError):
                with self.subTest(kind=kind, error=error.__name__):
                    effects, entered = [], False
                    def runner(args, **kwargs):
                        output = postgres.podman(args, **kwargs)
                        creation = create_resource(args)
                        if creation:
                            effects.append(creation[0])
                            if creation[0] == kind:
                                if error is subprocess.TimeoutExpired:
                                    raise error(["podman", kind, "create"], 60)
                                raise error("injected lost create acknowledgement")
                        return output
                    stack = postgres.DisposablePostgres(runner=runner)
                    with self.assertRaises(error):
                        with stack:
                            entered = True
                    self.assertFalse(entered)
                    self.assertIn(kind, effects)
                    self.assertTrue(stack.cleanup_verified)
                    self.assertEqual(stack.owned, set())
                    self.assertIsNone(stack.password)
                    self.assertIsNone(stack.lock)
                    for args in (["ps", "-a", "--format", "{{.Names}}"],
                                 ["volume", "ls", "--format", "{{.Name}}"],
                                 ["secret", "ls", "--format", "{{.Name}}"]):
                        self.assertNotIn(stack.name, postgres.podman(args).splitlines())
                    print("B01_CREATE_FAILURE_PROBE=" + json.dumps({
                        "kind": kind, "error": error.__name__, "created": effects,
                        "removed": True, "credential_discarded": True, "lock_released": True}))


if __name__ == "__main__":
    unittest.main()
