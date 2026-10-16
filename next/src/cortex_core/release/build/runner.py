"""One failure-safe lifecycle shared by product operations; no actor-granted PASS."""

from .compare import require_independent, compare_archives, compare_closure


def lifecycle(stages, operate, custody, cleanup, record):
    results = {}
    failure = None
    custody_result = None
    cleanup_result = None
    custody_error = None
    cleanup_error = None
    try:
        for stage in stages:
            results[stage] = operate(stage)
            observed = results[stage]
            if isinstance(observed, dict) and "exit" in observed:
                if type(observed["exit"]) is not int or observed["exit"] != 0:
                    raise ValueError("operation failed: " + stage)
    except BaseException as error:
        failure = type(error).__name__
    finally:
        try:
            custody_result = custody()
        except BaseException as error:
            custody_error = type(error).__name__
        finally:
            try:
                cleanup_result = cleanup()
            except BaseException as error:
                cleanup_error = type(error).__name__
    passed = (
        bool(results)
        and failure is None
        and custody_error is None
        and cleanup_error is None
        and custody_result is not None
        and custody_result.get("complete") is True
        and cleanup_result is not None
        and cleanup_result.get("complete") is True
        and cleanup_result.get("remaining_owned_resources") == []
    )
    result = {
        "result": "PASS" if passed else "RED",
        "stages": results,
        "reason": failure or custody_error or cleanup_error,
        "custody": custody_result,
        "cleanup": cleanup_result,
    }
    record(result)
    return result


def verify_pair(a, b):
    require_independent(a["identity"], b["identity"])
    if a["exit"] != 0 or b["exit"] != 0:
        raise ValueError("independent build did not succeed")
    compare_archives(a["archive"], b["archive"])
    compare_closure(a["oci"], b["oci"])
    if a["audit"] != b["audit"] or a["audit"].get("complete") is not True:
        raise ValueError("semantic audit incomplete or differs")
    if a["cleanup"] is not True or b["cleanup"] is not True:
        raise ValueError("build cleanup missing")
    return {
        "raw_archives_equal": True,
        "oci_closure_equal": True,
        "semantic_audit_equal": True,
    }


def plan_source_recipes(raw_envelopes, requests, fact_reader, roots, limits):
    """Nonexecuting recipe SOURCE entry; never the native producer."""
    from .envelopes import plan_source_recipes as plan

    return plan(raw_envelopes, requests, fact_reader, roots, limits)


# ---------------------------------------------------------------------------
# R4: one native lane (accepted note v3). lifecycle(input..audit) + ONE cleanup in
# finally; every phase is an aggregate of ordered children through the one adapter,
# all drawing on one lane Deadline (phase windows + cleanup reserve) and one
# OutputAccount (disk_bytes, cleanup byte reserve). Raw producer evidence only.

NATIVE_STAGES = ("input", "parser", "build", "export", "audit")
CLASSES = ("accounts", "cache", "clock", "files", "input", "labels", "logs",
           "python", "runtime", "serialization", "xattrs-acls")
METADATA_CLASSES = ("accounts", "cache", "clock", "files", "labels", "logs",
                    "python", "serialization", "xattrs-acls")
LAYER_TAR = "application/vnd.oci.image.layer.v1.tar"
KILL_SECONDS = 10


def _now():
    import time

    return time.monotonic()


def _child(argv, cwd, environment, timeout, account, stem):
    """One lane child through the one adapter, charged to the lane's account."""
    from pathlib import Path
    from .engine import run_operation

    out, err = Path(str(stem) + ".out"), Path(str(stem) + ".err")
    result = run_operation(argv, cwd, environment, timeout, out, err,
                           account=account, kill_seconds=KILL_SECONDS)
    return result["exit"], out.read_bytes(), {"stdout": out, "stderr": err,
                                               "journal": result["journal"]}


def _runtime_smoke(context):
    """The runtime class needs Cox's minimal smoke contract (B/R0). Until it is
    supplied the class is MISSING, which refuses raw PASS; never a clean report."""
    return "missing", "runtime smoke contract not supplied"


class _EvidenceInspection:
    """Producer-side cache judge for this lane's own audit, EVIDENCE ONLY (Vera
    R4-T1): it calls inspector.collect_inspection_evidence and never
    admit_inspection, _session or any R01 session path."""

    def __init__(self, closure, profile, deadline, account, evidence_dir=None, children=None):
        self.closure, self.profile = closure, profile
        self.deadline, self.account, self.records = deadline, account, []
        self.evidence_dir, self.children = evidence_dir, children if children is not None else []

    def check(self, rows):
        import time
        from pathlib import Path
        from cortex_core.release.codec import canonical_bytes, sha256
        from cortex_core.release.contracts import ReceiptRefusal
        from cortex_core.release.inspector import ENGINE_ENVIRONMENT, collect_inspection_evidence

        started = time.time_ns()
        nested = []
        try:
            record = collect_inspection_evidence(self.closure, self.profile, rows, deadline=self.deadline,
                                                 account=self.account, evidence_dir=self.evidence_dir, history=nested)
        except BaseException as error:
            # Hale R4-MAP-001: a failed nested run publishes its call and its REAL history too
            self._publish(started, nested, None, canonical_bytes({"error": type(error).__name__}),
                          (type(error).__name__ + ": " + str(error))[:512], None)
            raise
        self.records.append(record)
        nested = list(record.get("children") or nested)
        self._publish(started, nested, record["argv"], record.get("reply") or canonical_bytes(record["verdicts"]), None,
                      record.get("request"))
        if not nested:
            raise ReceiptRefusal("inspector_run_incomplete")
        if not all(row["equal"] is True for row in record["verdicts"]):
            raise ReceiptRefusal("python_compilation_policy")

    def _publish(self, started, nested, argv, stdout, error, request):
        """The evidence call itself, then its real nested children (success or failure)."""
        import time
        from pathlib import Path
        from cortex_core.release.codec import canonical_bytes, sha256
        from cortex_core.release.inspector import ENGINE_ENVIRONMENT

        if argv is None:
            runs = [child["argv"] for child in nested if child["argv"][8:9] == ["run"]]
            argv = runs[0] if runs else ["inspector-evidence-call"]
        session = argv[3][:-len("/store")] if len(argv) > 3 and argv[3].endswith("/store") else str(self.evidence_dir)
        call = {"argv": list(argv), "cwd": session, "environment_names": sorted(ENGINE_ENVIRONMENT),
                "exit": 0 if nested and error is None else None, "started_at_unix_ns": started,
                "finished_at_unix_ns": time.time_ns()}
        if error is not None:
            call["error"] = error
        index = len(self.children)
        streams = [("stdout", stdout), ("stderr", b""), ("journal", canonical_bytes(nested))]
        if request is not None:
            streams.append(("request", request))  # the request this reply answers (Cox R4-R01-001)
        for key, raw in streams:
            if self.evidence_dir is None:
                call[key] = {"path": None, "bytes": len(raw), "sha256": sha256(raw)}
                continue
            path = Path(self.evidence_dir) / ("audit-call-" + str(index) + "." + key)
            path.write_bytes(raw)
            call[key] = {"path": str(path), "bytes": len(raw), "sha256": sha256(raw)}
        self.children.append(call)
        self.children.extend(nested)


def native_lane(slot, verified, materialize, base, closure, host, parent,
                output_root, evidence_root, *, clock=None):
    """Run one native A|B lane; returns the honest lane record (PASS or RED).

    materialize(dest) creates the admitted clone (inputs.materialize in the
    product entry); base = {root, row, config_sha256} names R1's accepted base
    archive; closure is the admitted InspectorClosure for this host (it also
    carries the accepted engine/conf rows); host is the R8 record.
    """
    import os
    import secrets
    import sys
    import time
    from pathlib import Path
    from cortex_core.release.artifacts import inspect_oci
    from cortex_core.release.codec import canonical_bytes, decode_json, sha256
    from cortex_core.release.contracts import thaw
    from cortex_core.release.inspector import _read_member, _write_owned
    from . import _owned_remove, export
    from .engine import Deadline, OutputAccount, OwnedRoots
    from .inputs import require_verified
    from .policy import native_build_template, verify_native

    require_verified(verified)
    value = thaw(verified.document)
    metadata = thaw(verified.metadata)
    profile = metadata["release_policy"]
    # Hale R4-TEMPLATE-001: refuse before any root, load, info or build.
    if value["recipe"]["argv_template"] != native_build_template(value):
        raise ValueError("native_recipe_uncontained")
    verify_native(host, value["target"]["oci_platform"])
    resources = value["resources"]
    caps = resources["phase_timeouts_sec"]
    deadline = Deadline(resources["absolute_timeout_sec"], clock=clock or _now, reserve=caps["cleanup"])
    account = OutputAccount(resources["disk_bytes"], reserve=min(resources["disk_bytes"] // 4, 1 << 20))
    stem = "lane-" + slot + "-" + secrets.token_hex(8)
    parent = Path(parent)
    namespace = OwnedRoots([parent / (stem + "-store"), parent / (stem + "-runroot")]).create()
    plain = OwnedRoots([parent / (stem + "-clone"), parent / (stem + "-tmp"),
                        parent / (stem + "-layout")]).create()
    store, runroot = namespace.paths
    clone, tmp, layout = plain.paths
    out_dir, ev_dir = Path(output_root) / slot, Path(evidence_root) / slot
    out_dir.mkdir(mode=0o700)
    ev_dir.mkdir(mode=0o700)
    for name in ("home", "xdg"):
        (tmp / name).mkdir(mode=0o700)
    record = thaw(closure.descriptor)["descriptor"]
    environment = {"CONTAINERS_CONF": record["engine_config"]["containers_conf"]["path"],
                   "CONTAINERS_STORAGE_CONF": record["engine_config"]["storage_conf"]["path"],
                   "HOME": str(tmp / "home"), "PATH": "/usr/bin:/bin", "TMPDIR": str(tmp),
                   "XDG_RUNTIME_DIR": str(tmp / "xdg")}
    prefix = (str(closure.engine), "--remote=false", "--root", str(store), "--runroot", str(runroot),
              "--tmpdir", str(tmp))
    state = {"phases": {}, "classes": {}, "outputs": {}, "inspection": [], "iid": None,
             "engine": None, "oci": None}

    def measure(path):
        path = Path(path)
        if not path.is_file():
            return None
        raw = path.read_bytes()
        return {"path": str(path.relative_to(evidence_root)), "bytes": len(raw), "sha256": sha256(raw)}

    def run(phase, argv, cwd, timeout=None):
        children = state["phases"][phase]["children"]
        stem_path = ev_dir / (phase + "-" + str(len(children)))
        row = {"argv": list(argv), "cwd": str(cwd), "environment_names": sorted(environment),
               "started_at_unix_ns": time.time_ns()}
        try:
            # Hale R4-BUDGET-001: the child AND its TERM/KILL wait fit the fixed window.
            code, stdout, files = _child(list(argv), cwd, environment,
                                         deadline.child_timeout(KILL_SECONDS) if timeout is None else timeout,
                                         account, stem_path)
        except BaseException as error:
            # Hale R4-MAP-001(a): a failed or timed-out attempt stays in the ordered evidence.
            row.update(exit=None, error=(type(error).__name__ + ": " + str(error))[:512],
                       finished_at_unix_ns=time.time_ns(),
                       **{key: measure(Path(str(stem_path) + suffix)) for key, suffix in
                          (("stdout", ".out"), ("stderr", ".err"), ("journal", ".out.operation.jsonl"))})
            children.append(row)
            raise
        row.update(exit=code, finished_at_unix_ns=time.time_ns(),
                   **{key: measure(path) for key, path in files.items()})
        children.append(row)
        if code != 0:
            raise ValueError("operation failed: " + phase)
        return stdout

    def output(role, name, raw, media):
        path = out_dir / name
        _write_owned(path, raw, 0o444)
        state["outputs"][role] = {"role": role, "path": slot + "/" + name, "media_type": media,
                                  "bytes": len(raw), "sha256": sha256(raw)}

    def phase_input():
        materialize(clone)
        _, raw = _read_member(Path(base["root"]), dict(base["row"], role="image"), keep=True)
        copy = tmp / "base.tar"
        _write_owned(copy, raw, 0o444)
        run("input", prefix + ("load", "--input", str(copy)), tmp)
        shown = decode_json(run("input", prefix + ("image", "inspect", "--format", "json",
                                                   "sha256:" + base["config_sha256"]), tmp),
                            1 << 20, canonical=False)
        if not (type(shown) is list and len(shown) == 1 and type(shown[0]) is dict and
                shown[0].get("Id") in (base["config_sha256"], "sha256:" + base["config_sha256"])):
            raise ValueError("native_base_identity")

    def phase_parser():
        info = decode_json(run("parser", prefix + ("info", "--format", "json"), tmp), 1 << 20, canonical=False)
        if type(info) is not dict:
            raise ValueError("native_store_unqualified")
        security = (info.get("host") or {}).get("security") or {}
        if (info.get("store") or {}).get("graphRoot") != str(store) or security.get("rootless") is not True:
            raise ValueError("native_store_unqualified")
        required = value["builder_requirements"]["engine"]
        observed = {"engine_id": required["engine_id"], "version": (info.get("version") or {}).get("Version"),
                    "sha256": closure.engine_sha256}
        if observed != required:
            raise ValueError("native_engine_identity")
        state["engine"] = observed

    def phase_build():
        run("build", prefix + tuple(value["recipe"]["argv_template"]), clone)
        iid = (clone / "image.iid").read_text().strip()
        if len(iid) != 71 or not iid.startswith("sha256:") or any(c not in "0123456789abcdef" for c in iid[7:]):
            raise ValueError("native_image_identity")
        state["iid"] = iid

    def phase_export():
        run("export", prefix + ("image", "save", "--format", "oci-dir", "--uncompressed", "--output",
                                str(layout / "oci"), state["iid"]), tmp)
        files = export.snapshot_files(layout / "oci")
        index = export._json_object(files["index.json"])
        manifest_key = "blobs/sha256/" + index["manifests"][0]["digest"][7:]
        manifest = export._json_object(files[manifest_key])
        # Hale R4-MAP-001: refuse compressed layers before writing any archive.
        if not manifest["layers"] or any(layer.get("mediaType") != LAYER_TAR for layer in manifest["layers"]):
            raise ValueError("native_layer_compressed")
        export.write_archive(layout / "oci", tmp / "image.tar", value["determinism"]["epoch"])
        output("archive", "image.tar", (tmp / "image.tar").read_bytes(), "application/x-tar")
        output("index", "index.json", files["index.json"], "application/vnd.oci.image.index.v1+json")
        output("manifest", "manifest.json", files[manifest_key], "application/vnd.oci.image.manifest.v1+json")
        config_key = manifest["config"]["digest"][7:]
        output("config", "config.json", files["blobs/sha256/" + config_key], "application/vnd.oci.image.config.v1+json")
        layers = [layer["digest"][7:] for layer in manifest["layers"]]
        for key in layers:
            output("blob." + key, "layer-" + key + ".tar", files["blobs/sha256/" + key], "application/octet-stream")
        state["oci"] = {"index": sha256(files["index.json"]), "manifest": manifest_key[13:], "config": config_key,
                        "blobs": sorted(set(layers + [manifest_key[13:], config_key])),
                        "platform": value["target"]["oci_platform"]}

    def phase_audit():
        classes = {name: ("missing", "not checked") for name in CLASSES}
        classes["input"] = ("pass", "admitted input and clone")
        judge = _EvidenceInspection(closure, profile, deadline, account, ev_dir,
                                    state["phases"]["audit"]["children"])
        try:
            records = list(state["outputs"].values())
            records += [{"role": role, "path": slot + "/" + role + ".json", "media_type": "application/json",
                         "bytes": 0, "sha256": "0" * 64} for role in ("sbom", "provenance")]
            actual = {row["path"]: (Path(output_root) / row["path"]).read_bytes()
                      for row in state["outputs"].values()}
            _, inventory = inspect_oci(actual[state["outputs"]["archive"]["path"]], records, actual,
                                       state["oci"], value, module_policy=profile,
                                       accepted_inputs=metadata["accepted_inputs"], inspection=judge)
            state["inventory"] = inventory
            for name in METADATA_CLASSES:
                classes[name] = ("pass", "R01 image reader")
        except (ValueError, KeyError, TypeError, OSError) as error:
            # any failure of a required check is a failed class, never a skipped one
            for name in METADATA_CLASSES:
                classes[name] = ("failed", type(error).__name__ + ": " + str(error))
        classes["runtime"] = _runtime_smoke(state)
        state["inspection"] = judge.records
        for child in state["phases"]["audit"]["children"]:
            for key in ("stdout", "stderr", "journal", "request"):
                stream = child.get(key)
                if stream and stream.get("path") and Path(stream["path"]).is_absolute():
                    stream["path"] = str(Path(stream["path"]).relative_to(evidence_root))
        state["classes"] = classes

    phases = {"input": phase_input, "parser": phase_parser, "build": phase_build,
              "export": phase_export, "audit": phase_audit}

    def operate(stage):
        deadline.enter_phase(caps[stage])
        state["phases"][stage] = {"started_at_unix_ns": time.time_ns(), "children": []}
        try:
            phases[stage]()
        finally:
            state["phases"][stage]["finished_at_unix_ns"] = time.time_ns()
        return {"exit": 0}

    def custody():
        measured = [row for row in state["outputs"].values()
                    if sha256((Path(output_root) / row["path"]).read_bytes()) == row["sha256"]]
        complete = state["oci"] is not None and len(measured) == len(state["outputs"])
        return {"complete": complete, "outputs": sorted(measured, key=lambda row: row["path"])}

    def cleanup():
        account.cleanup()
        deadline.enter_cleanup(caps["cleanup"])
        state["phases"]["cleanup"] = {"started_at_unix_ns": time.time_ns(), "children": []}
        absent = removed = False
        removed_ids = []

        def op(argv):
            timeout = deadline.cleanup_timeout(KILL_SECONDS)
            if timeout <= 0:
                raise TimeoutError("cleanup reserve spent")
            return run("cleanup", argv, tmp, timeout)

        def listed():
            rows = decode_json(op(prefix + ("ps", "-a", "--external", "--format", "json")) or b"[]",
                               1 << 20, canonical=False)
            if type(rows) is not list or not all(type(row) is dict and type(row.get("Id")) is str and
                                                  len(row["Id"]) == 64 for row in rows):
                raise ValueError("container discovery unparsable")
            return sorted({row["Id"] for row in rows})

        def same():
            try:
                return all(OwnedRoots.identity(path.stat(follow_symlinks=False)) == namespace.created[path]
                           for path in namespace.paths)
            except OSError:
                return False

        try:
            # Hale R4-OWN-001: prove the recorded store/runroot identities BEFORE any
            # discovery or removal can target a store; on doubt, no engine command at all.
            if not same():
                raise ValueError("owned store identity changed")
            # discover every container in the proven private store (including ones the
            # client never journaled), remove by exact ID, prove absence
            removed_ids = listed()
            if removed_ids:
                op(prefix + ("rm", "--force", "--time", "0", *removed_ids))
            absent = listed() == []
            if absent and same():
                targets = [str(part) for path in namespace.paths
                           for part in (path, *namespace.created[path][:2])]
                op(prefix + ("unshare", sys.executable, "-I", "-S", "-B", _owned_remove.__file__, *targets))
                removed = not any(path.exists() for path in namespace.paths)
        except Exception:
            removed = False
        plain_result = plain.cleanup() if removed else {"complete": False}
        state["phases"]["cleanup"]["finished_at_unix_ns"] = time.time_ns()
        remaining = [str(path) for path in (*namespace.paths, *plain.paths) if path.exists()]
        return {"complete": bool(absent and removed and plain_result.get("complete") is True and not remaining),
                "remaining_owned_resources": remaining, "removed_container_ids": removed_ids}

    result = lifecycle(NATIVE_STAGES, operate, custody, cleanup, lambda row: None)
    for phase, row in state["phases"].items():
        _write_owned(ev_dir / (phase + ".children.json"), canonical_bytes(row["children"]), 0o444)
    classes = state["classes"] or {name: ("missing", "audit not reached") for name in CLASSES}
    complete = result["result"] == "PASS" and all(status == "pass" for status, _ in classes.values())
    return {"slot": slot, "result": "PASS" if complete else "RED", "lifecycle": result,
            "phases": state["phases"], "classes": classes, "outputs": state["outputs"], "oci": state["oci"],
            "engine": state["engine"], "inspection": state["inspection"], "iid": state["iid"],
            "inventory": state.get("inventory"),
            "identities": {"store_id": str(store), "runroot_id": str(runroot), "source_id": str(clone),
                           "execution_id": stem}}


def _build_record(lane, value, host, evidence_root, output_root):
    """Receipt build record for one complete lane, written as original evidence."""
    from pathlib import Path
    from cortex_core.release.codec import canonical_bytes, sha256
    from cortex_core.release.inspector import _write_owned

    slot = lane["slot"]
    build_id = lane["identities"]["execution_id"]
    policy = value["admission_policy"]

    def artifact(root, role, name, raw, media="application/json"):
        _write_owned(Path(root) / name, raw, 0o444)
        return {"role": role, "path": name, "media_type": media, "bytes": len(raw), "sha256": sha256(raw)}

    observed = {"qualification_scope": "native", "os": "linux",
                "architecture": value["builder_requirements"]["architecture"], "kernel": host["kernel"],
                "uid": host["uid"], "rootless": True, "toolchain": value["builder_requirements"]["toolchain"],
                "engine": lane["engine"], "build_id": build_id, "execution_id": build_id,
                "store_id": lane["identities"]["store_id"], "runroot_id": lane["identities"]["runroot_id"],
                "container_id": lane["iid"], "source_id": lane["identities"]["source_id"],
                "fresh_store": True, "mutable_shared": False}
    components = {"source_sha256": sha256(canonical_bytes(value["source"])),
                  "recipe_sha256": sha256(canonical_bytes(value["recipe"])),
                  "dependency_sha256": sha256(canonical_bytes(value["dependencies"]))}
    operations = []
    for phase in ("input", "parser", "build", "export", "audit", "cleanup"):
        row = lane["phases"][phase]
        children = row["children"]
        argv = (value["recipe"]["argv_template"] if phase == "build"
                else children[0]["argv"] if children else ["controller", phase])
        op = {"phase": phase, "argv": argv, "environment_names": value["determinism"]["environment"]["names"],
              "environment_policy_sha256": policy["policy_sha256"], "status": "exited", "exit_code": 0,
              "started_at_unix_ns": row["started_at_unix_ns"], "finished_at_unix_ns": row["finished_at_unix_ns"]}
        raw = canonical_bytes(dict(op, build_id=build_id, qualification_scope="native"))
        operations.append(dict(op, evidence=artifact(evidence_root, slot + ".operation." + phase,
                                                     slot + "/" + phase + ".json", raw)))
    archive = lane["outputs"]["archive"]
    inventory_sha = sha256(canonical_bytes(lane["inventory"]))
    outputs = list(lane["outputs"].values())
    outputs.append(artifact(output_root, "sbom", slot + "/sbom.json",
                            canonical_bytes({"output_sha256": archive["sha256"], "inventory_sha256": inventory_sha})))
    provenance = artifact(output_root, "provenance", slot + "/provenance.json", canonical_bytes(
        {"build_id": build_id, "input_sha256": sha256(canonical_bytes(value)), "output_sha256": archive["sha256"],
         **components, "policy_sha256": policy["policy_sha256"], "observed_builder": observed,
         "qualification_scope": "native"}))
    outputs.append(provenance)
    outputs.sort(key=lambda row: row["path"])
    reports = {name: artifact(evidence_root, slot + ".audit." + name, slot + "/audit-" + name + ".json",
                              canonical_bytes({"build_id": build_id, "class": name,
                                               "output_sha256": archive["sha256"],
                                               "inventory_sha256": inventory_sha, "findings": []}))
               for name in CLASSES}
    custody = artifact(evidence_root, slot + ".custody", slot + "/custody.json",
                       canonical_bytes({"build_id": build_id, "outputs": outputs, "complete": True}))
    resources = [observed[key] for key in ("execution_id", "store_id", "runroot_id", "container_id", "source_id")]
    cleanup = artifact(evidence_root, slot + ".cleanup", slot + "/cleanup-result.json",
                       canonical_bytes({"build_id": build_id, "removed_resources": resources,
                                        "remaining_owned_resources": [], "complete": True}))
    starts = [op["started_at_unix_ns"] for op in operations]
    ends = [op["finished_at_unix_ns"] for op in operations]
    build = {"build_id": build_id, "observed_builder": observed, **components,
             "started_at_unix_ns": min(starts), "finished_at_unix_ns": max(ends), "operations": operations,
             "outputs": outputs, "oci": lane["oci"], "provenance": provenance, "custody": custody, "cleanup": cleanup}
    children = [{"role": slot + ".children." + phase, "path": slot + "/" + phase + ".children.json",
                 "media_type": "application/json", **_measured(Path(evidence_root) / slot / (phase + ".children.json"))}
                for phase in ("input", "parser", "build", "export", "audit", "cleanup")]
    streams = [{"role": slot + ".stream." + phase + "." + str(index) + "." + key, "path": child[key]["path"],
                "media_type": "application/octet-stream", "bytes": child[key]["bytes"], "sha256": child[key]["sha256"]}
               for phase in ("input", "parser", "build", "export", "audit", "cleanup")
               for index, child in enumerate(lane["phases"][phase]["children"])
               for key in ("stdout", "stderr", "journal", "request") if key in child]
    evidence = ([op["evidence"] for op in operations] + list(reports.values()) + [custody, cleanup] + children +
                streams)
    return build, reports, evidence


def _measured(path):
    from cortex_core.release.codec import sha256

    raw = path.read_bytes()
    return {"bytes": len(raw), "sha256": sha256(raw)}


def native_lanes(verified, materialize, base, closure, host, parent, output_root, evidence_root,
                 qualification, *, clock=None):
    """The one native product entry (R4 -> R5): lanes A then B (sequential, independent
    roots), then, only if both are complete, the RAW candidate receipt from emit()."""
    from pathlib import Path
    from cortex_core.release.codec import canonical_bytes, sha256
    from cortex_core.release.contracts import thaw
    from cortex_core.release.inspector import _write_owned
    from . import receipts

    lanes = {slot: native_lane(slot, verified, materialize, base, closure, host, parent, output_root,
                               evidence_root, clock=clock) for slot in ("A", "B")}
    if any(lane["result"] != "PASS" for lane in lanes.values()):
        return {"result": "RED", "lanes": lanes, "receipt": None}
    value = thaw(verified.document)
    builds, reports, evidence = {}, {}, []
    for slot, lane in lanes.items():
        builds[slot], reports[slot], rows = _build_record(lane, value, host, evidence_root, output_root)
        evidence += rows
    # The R6 record and every original it cites (each row's request and real child
    # records, plus the streams those records name), which the R6 operator placed in
    # the evidence root, join the exact-set evidence inventory (Cox R4-R01-001).
    # Missing ones stay absent, so emit (and later R01) refuse.
    from cortex_core.release.codec import decode_json
    from cortex_core.release.receipts import qualification_complete, qualification_paths, qualification_streams

    record = qualification
    _write_owned(Path(evidence_root) / "qualification.json", record, 0o444)
    evidence.append({"role": "native.qualification", "path": "qualification.json", "media_type": "application/json",
                     "bytes": len(record), "sha256": sha256(record)})
    try:
        cited = qualification_paths(qualification_complete(decode_json(record, 1024 * 1024)))
    except ValueError:
        cited = []
    originals = {}
    for name in cited:
        source = Path(evidence_root) / name
        if source.is_file() and not source.is_symlink():
            originals[name] = source.read_bytes()
            if name.endswith(".children.json"):
                try:
                    streams = qualification_streams(originals[name])
                except ValueError:
                    streams = []
                for path in streams:
                    stream = Path(evidence_root) / path
                    if stream.is_file() and not stream.is_symlink():
                        originals[path] = stream.read_bytes()
    for name, raw in sorted(originals.items()):
        evidence.append({"role": "native.qualification.original", "path": name,
                         "media_type": "application/octet-stream", "bytes": len(raw), "sha256": sha256(raw)})
    outputs = sorted([row for slot in builds for row in builds[slot]["outputs"]], key=lambda row: row["path"])
    def record(role, name, raw):
        _write_owned(Path(evidence_root) / name, raw, 0o444)
        return {"role": role, "path": name, "media_type": "application/json", "bytes": len(raw), "sha256": sha256(raw)}
    custody = record("custody", "custody.json", canonical_bytes(
        {"complete": True, "partial": False, "missing_roles": [], "outputs": outputs}))
    cleanup = record("cleanup", "cleanup.json", canonical_bytes(
        {"build_ids": [builds[s]["build_id"] for s in ("A", "B")], "complete": True, "remaining_owned_resources": []}))
    evidence += [custody, cleanup]
    evidence.sort(key=lambda row: row["path"])
    index = record("evidence-index", "index.json", canonical_bytes({"inventory": evidence}))
    policy = value["admission_policy"]
    audit = {"policy_id": policy["policy_id"], "policy_sha256": policy["policy_sha256"],
             "required_classes": list(CLASSES), "reports": reports, "findings": []}
    raw, verified_receipt = receipts.emit(
        verified, builds, audit, {"inventory": evidence, "index": index},
        {"complete": True, "partial": False, "missing_roles": [], "receipt": custody},
        {"complete": True, "receipt": cleanup, "remaining_owned_resources": []},
        lambda path: (Path(output_root) / path).read_bytes(), lambda path: (Path(evidence_root) / path).read_bytes(),
        None)
    return {"result": "RAW", "lanes": lanes, "receipt": raw, "verified": verified_receipt}


QUALIFICATION_DIR = "q"


def _qualification_row(evidence, kind, request, children):
    """One row's originals in the evidence root: its request and the REAL child records
    of its run (inspector._Children), stream paths made relative. Returns the refs."""
    from pathlib import Path
    from cortex_core.release.codec import canonical_bytes
    from cortex_core.release.inspector import _write_owned

    evidence = Path(evidence)
    for child in children:
        for key in ("stdout", "stderr", "journal"):
            stream = child.get(key)
            if stream and stream.get("path") and Path(stream["path"]).is_absolute():
                stream["path"] = str(Path(stream["path"]).relative_to(evidence))
    base = QUALIFICATION_DIR + "/" + kind
    _write_owned(evidence / (base + ".request"), request, 0o444)
    _write_owned(evidence / (base + ".children.json"), canonical_bytes(children), 0o444)
    return {"request": base + ".request", "children": base + ".children.json"}


def qualify(verified, materialize, base, closure, probe, host, parent, root, allowlist, *, clock=None):
    """R6 run 2, steps c-f for one arch (note v2 ORDER): ONE preliminary lane (slot P)
    whose evidence-only audit IS the positive; the negative (exactly one cache body
    byte flipped, through collect_inspection_evidence); the seven probes (purpose=probe,
    each its own fresh store); then the record (references only). The shared R01 row
    function must accept EVERY row here, else no record and no native_lanes call.
    P-lane outputs and roots are unqualified material: never assembled or emitted.
    Returns (record bytes, {evidence path: original bytes})."""
    import base64
    import sys
    from pathlib import Path
    from cortex_core.release.codec import canonical_bytes, decode_json, sha256
    from cortex_core.release.contracts import ReceiptRefusal, thaw
    from cortex_core.release.inspector import _request, collect_inspection_evidence, run_probe
    from cortex_core.release.receipts import (PROBES, QUALIFICATION_SCHEMA, qualification_complete,
                                              qualification_paths, qualification_row, qualification_streams)
    from . import _owned_remove

    fail = "native_inspector_execution_unqualified"
    root = Path(root)
    evidence, material = root / "evidence", root / "material"
    for path in (evidence, material, evidence / QUALIFICATION_DIR):
        path.mkdir(mode=0o700)
    value = thaw(verified.document)
    profile = thaw(verified.metadata)["release_policy"]
    lane = native_lane("P", verified, materialize, base, closure, host, parent, material, evidence, clock=clock)
    if len(lane["inspection"]) != 1:
        raise ReceiptRefusal(fail)  # no corpus: nothing to qualify against
    corpus = lane["inspection"][0]
    rows = {"positive": _qualification_row(evidence, "positive", corpus["request"], corpus["children"])}
    request = decode_json(corpus["request"], profile["inspector"]["limits"]["input_bytes"])
    cases = [{"cache": row["cache"], "source": row["source"], "final_filename": row["final_filename"],
              "optimization": request["optimization"], "cache_bytes": base64.b64decode(row["cache_b64"]),
              "source_bytes": base64.b64decode(row["source_b64"])} for row in request["rows"]]
    body = cases[0]["cache_bytes"]
    cases[0]["cache_bytes"] = body[:-1] + bytes([body[-1] ^ 1])
    (evidence / QUALIFICATION_DIR / "negative").mkdir(mode=0o700)
    nested = []
    try:
        negative = collect_inspection_evidence(closure, profile, cases, evidence_dir=evidence / QUALIFICATION_DIR / "negative",
                                               history=nested)["request"]
    except ReceiptRefusal:
        negative = _request(closure, profile, cases)[0]  # the failed run's history still becomes evidence
    rows["negative"] = _qualification_row(evidence, "negative", negative, nested)
    for name in PROBES:
        (evidence / QUALIFICATION_DIR / name).mkdir(mode=0o700)
        result = run_probe(probe, profile, name, evidence_dir=evidence / QUALIFICATION_DIR / name)
        rows[name] = _qualification_row(evidence, name, result["request"], result["children"])
    descriptor = thaw(closure.descriptor)["descriptor"]
    doc = {"schema": QUALIFICATION_SCHEMA, "architecture": value["builder_requirements"]["architecture"],
           "platform": value["target"]["oci_platform"],
           "host": {key: host.get(key) for key in ("os", "arch", "uid", "rootless", "emulated", "kernel")},
           "engine": lane["engine"], "engine_path": str(closure.engine),
           "engine_config": {name: descriptor["engine_config"][name]["sha256"] for name in ("containers_conf", "storage_conf")},
           "inspector": {"closure_sha256": closure.closure_sha256, "positive": rows["positive"], "negative": rows["negative"]},
           "probe_image": probe.image_digest, "containment": {name: rows[name] for name in PROBES},
           "cleanup_interpreter": sys.executable,
           "cleanup_script": {"path": _owned_remove.__file__, "sha256": sha256(Path(_owned_remove.__file__).read_bytes())}}
    files = {}
    try:
        qualification_complete(doc)
        for path in qualification_paths(doc):
            files[path] = (evidence / path).read_bytes()
            if path.endswith(".children.json"):
                for stream in qualification_streams(files[path]):
                    files[stream] = (evidence / stream).read_bytes()
        # step f: the SAME pure row function R01 uses, producer-side, before any lane call
        context = {"capability": closure, "probe": probe, "descriptor": descriptor, "profile": profile,
                   "input_value": value, "doc": doc, "allowlist": allowlist, "positive": None}
        def check(kind, cited):
            children = decode_json(files[cited["children"]], 8 * 1024 * 1024)
            return qualification_row(kind, files[cited["request"]], children, files, context)[1]
        context["positive"] = check("positive", rows["positive"])
        check("negative", rows["negative"])
        for name in PROBES:
            check(name, rows[name])
    except (ValueError, KeyError, TypeError, OSError):
        raise ReceiptRefusal(fail) from None
    return canonical_bytes(doc), files


def operate(modules, closure, probe, host, parent, root, allowlist, *, clock=None):
    """R6 run 2 for one arch, the exact call sequence: qualify (steps c-f, the corpus is
    the Core input) and only then native_lanes(Core), native_lanes(API) (step g): TWO
    calls, each running A then B internally, each with the record's originals written
    into its own evidence root. modules[name] = {verified, materialize, base,
    output_root, evidence_root}."""
    from pathlib import Path
    from cortex_core.release.inspector import _write_owned

    core = modules["Core"]
    record, files = qualify(core["verified"], core["materialize"], core["base"], closure, probe, host, parent, root,
                            allowlist, clock=clock)
    results = {}
    for name in ("Core", "API"):
        module = modules[name]
        for path, raw in sorted(files.items()):
            target = Path(module["evidence_root"]) / path
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            _write_owned(target, raw, 0o444)
        results[name] = native_lanes(module["verified"], module["materialize"], module["base"], closure, host, parent,
                                     module["output_root"], module["evidence_root"], record, clock=clock)
    return {"record": record, "lanes": results}


# R6-LIFETIME-001: ONE bounded operator run identity. Stage budgets (seconds); the
# lanes stage charges all FIVE lane windows (P + Core A/B + API A/B, kai R6 clause).
OPERATOR_STAGES_SEC = (("setup", 1200), ("assets", 1200), ("qualification", 1800))
OPERATOR_LANE_WINDOWS = 5
OPERATOR_TEARDOWN_SEC = 1200
UNIT_DIR = "/run/systemd/system"


def operator_budget(absolute_timeout_sec):
    """The reconciled run budget: named stages, the teardown reserve, and their total
    (= the unit's RuntimeMaxSec). Refuses anything but a positive integer timeout."""
    if type(absolute_timeout_sec) is not int or absolute_timeout_sec <= 0:
        raise ValueError("operator_budget_unreconciled")
    stages = dict(OPERATOR_STAGES_SEC)
    stages["lanes"] = OPERATOR_LANE_WINDOWS * absolute_timeout_sec
    stages["teardown"] = OPERATOR_TEARDOWN_SEC
    return {"stages": stages, "total_sec": sum(stages.values())}


def operator_deadline(budget, *, clock=None):
    """The ONE run Deadline: total budget, the last OPERATOR_TEARDOWN_SEC for teardown."""
    from .engine import Deadline

    if budget.get("total_sec") != sum(budget["stages"].values()) or budget["stages"].get("teardown") != OPERATOR_TEARDOWN_SEC:
        raise ValueError("operator_budget_unreconciled")
    return Deadline(budget["total_sec"], clock=clock or _now, reserve=OPERATOR_TEARDOWN_SEC)


def operator_unit(name, budget, exec_start, exec_stop_post):
    """The transient root unit text (lab rule: a unit file in /run/systemd/system, never
    systemd-run). RuntimeMaxSec = the reconciled total; ExecStopPost runs the finally
    on every exit including the timeout. Returns (path, text)."""
    import re

    token = re.compile(r"[A-Za-z0-9_./=:,+@-]+")
    if (not re.fullmatch(r"cortex-r6-[a-z0-9-]{1,64}", name) or budget.get("total_sec") != sum(budget["stages"].values()) or
            not all(type(argv) is list and argv and argv[0].startswith("/") and
                    all(type(part) is str and token.fullmatch(part) for part in argv)
                    for argv in (exec_start, exec_stop_post))):
        raise ValueError("operator_unit_refused")
    text = ("[Unit]\nDescription=Cortex R6 native qualification run " + name + "\n\n[Service]\nType=exec\n"
            "RuntimeMaxSec=" + str(budget["total_sec"]) + "\nExecStart=" + " ".join(exec_start) +
            "\nExecStopPost=" + " ".join(exec_stop_post) + "\n")
    return UNIT_DIR + "/" + name + ".service", text


def operator_finally(steps):
    """The guest-side finally (ExecStopPost): EVERY step attempted in order (stop work,
    prove owned containers/processes absent, close results/custody, stop the session,
    remove the user, reconcile packages); a failure is kept, never skips the rest."""
    residuals = []
    for name, step in steps:
        try:
            if step() is not True:
                residuals.append(name)
        except Exception as error:
            residuals.append(name + ": " + type(error).__name__)
    return residuals


def operator_drive(start, read_state, deallocate, readback, *, polls):
    """The outer driver: start the named unit ONCE, poll it within a bounded window,
    then ALWAYS request deallocation and read it back, whatever happened before. An
    unknown state, a lost connection or an exhausted window is HOLD (reconcile by the
    unit name), never a duplicate start. Returns {result, unit, deallocated}."""
    unit = None
    try:
        start()
        unit = "unknown"
        for _ in range(polls):
            unit = read_state()
            if unit not in ("activating", "active", "deactivating"):
                break
    except Exception:
        unit = unit or "start-failed"
    try:
        deallocate()
    except Exception:
        pass
    try:
        deallocated = readback() == "deallocated"
    except Exception:
        deallocated = False
    if not deallocated or unit not in ("inactive", "failed", "start-failed"):
        result = "HOLD"
    else:
        result = "PASS" if unit == "inactive" else "RED"
    return {"result": result, "unit": unit, "deallocated": deallocated}


def _package_name(nevra):
    return nevra.rsplit("-", 2)[0]


def package_preview(baseline, preview):
    """`dnf install --assumeno` plan rows (action, nevra): only fresh installs of
    packages absent from the baseline; any upgrade/downgrade/removal or a preexisting
    package refuses (no approved disposition exists)."""
    names = {_package_name(nevra) for nevra in baseline}
    if not preview or not all(type(row) in (list, tuple) and len(row) == 2 and row[0] == "install" and
                              row[1] not in baseline and _package_name(row[1]) not in names for row in preview):
        raise ValueError("package_ownership_hold")
    return sorted(row[1] for row in preview)


def package_receipt(baseline, last_id_before, after, history):
    """The owned delta = exactly OUR transaction (kai R6 clause): the history's last id
    at baseline; afterwards exactly ONE new transaction, id = that + 1, whose action set
    equals after-minus-baseline, all installs, and the baseline kept whole. Any
    intervening transaction or mismatch refuses ownership and cleanup (HOLD)."""
    new = [row for row in history if row["id"] > last_id_before]
    delta = sorted(set(after) - set(baseline))
    if not (type(last_id_before) is int and len(new) == 1 and new[0]["id"] == last_id_before + 1 and
            set(baseline) <= set(after) and delta and
            all(type(row) in (list, tuple) and len(row) == 2 and row[0] == "install" for row in new[0]["actions"]) and
            sorted(row[1] for row in new[0]["actions"]) == delta):
        raise ValueError("package_ownership_hold")
    return {"transaction_id": new[0]["id"], "delta": delta,
            "cleanup_argv": ["dnf", "history", "undo", "--assumeyes", str(new[0]["id"])]}


def package_undo_preview(receipt, preview):
    """The undo's resolved action set, checked BEFORE it runs: exactly removing the delta."""
    if sorted(tuple(row) for row in preview) != sorted(("remove", nevra) for nevra in receipt["delta"]):
        raise ValueError("package_ownership_hold")
    return receipt["cleanup_argv"]


def package_readback(baseline, after_cleanup, user_absent):
    """Clean host only when rpm -qa == baseline AND the build user/session are gone."""
    if sorted(after_cleanup) != sorted(baseline) or user_absent is not True:
        raise ValueError("package_ownership_hold")
    return "clean"
