#!/usr/bin/env python3
"""Gate A run8 guarded packet CLI. Templates cannot authorize effects."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

from admission import load_authority, validate_release
from custody import Refusal, bound_json, measured_file, parse_json, require
from dispatch import execute
from keys import decoded_outcome
from observations import ObservationStore
from native_receipts import admit as admit_native
from payload import literal_values, validate_literals
from workflow import Workflow


def file_row(path, expected):
    measured = measured_file(Path(path))
    require(measured["sha256"] == expected, "independent_expected_hash_mismatch")
    return measured


def authority_args(parser):
    parser.add_argument("--binding", required=True)
    parser.add_argument("--binding-sha256", required=True)
    parser.add_argument("--go", required=True)
    parser.add_argument("--go-sha256", required=True)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    commands = p.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect-template")
    inspect.add_argument("--binding", required=True)
    key = commands.add_parser("classify-console")
    key.add_argument("--input", required=True, help="public captured console output only")
    key.add_argument("--representation", choices=["donor-auto", "decoded", "base64"], default="donor-auto")
    for name in ("preflight", "literal-shell", "init-observations", "show-observations", "execute-role", "observe"):
        q = commands.add_parser(name)
        authority_args(q)
        if name == "preflight":
            q.add_argument("--checks", required=True, help="file-record map of fresh measured checks")
            q.add_argument("--observations", help="append checked PRE_ALLOC; directory must already be initialized")
        if name == "literal-shell":
            q.add_argument("--delivery", required=True)
            q.add_argument("--delivery-sha256", required=True)
        if name in {"init-observations", "show-observations", "execute-role", "observe"}:
            q.add_argument("--observations", required=True)
        if name == "execute-role":
            q.add_argument("--role", required=True)
            q.add_argument("--invocations", required=True)
        if name == "observe":
            q.add_argument("--phase", choices=["ALLOCATED", "HOST_BOUND", "INSTALLED", "ARMED",
                                               "REBOOT_VERIFIED", "FAILURE", "RETIRED"], required=True)
            q.add_argument("--receipt", required=True)
            q.add_argument("--receipt-sha256", required=True)
    args = p.parse_args(argv)
    try:
        if args.command == "inspect-template":
            B = parse_json(Path(args.binding).read_bytes())
            unbound = sorted(k for k, v in B.items() if v is None)
            print(json.dumps({"schema": B.get("schema"), "execution_ready": False,
                              "qualification": False, "unbound_slots": unbound}))
            return 0
        if args.command == "classify-console":
            text = Path(args.input).read_text()
            outcome = decoded_outcome(text, args.representation)
            # A public host key itself is not needed in this typed caller receipt.
            outcome.pop("key", None)
            print(json.dumps(outcome))
            return outcome["exit"]
        A = load_authority(file_row(args.binding, args.binding_sha256),
                           file_row(args.go, args.go_sha256))
        if args.command == "preflight":
            validate_release(A)
            checks = parse_json(Path(args.checks).read_bytes())
            A.check_measured_receipts(checks)
            if args.observations:
                Workflow(A, ObservationStore(Path(args.observations), A), signature_runner=subprocess.run).preallocate(checks)
            print(json.dumps({"local_preflight": "PASS", **A.context,
                              "native_qualification": False, "allocation_performed": False}))
        elif args.command == "literal-shell":
            validate_release(A)
            actual = literal_values(A)
            validate_literals(A, actual, file_row(args.delivery, args.delivery_sha256))
            for key, value in actual.items():
                print("export " + key + "=" + shlex.quote(value))
        elif args.command == "init-observations":
            validate_release(A)
            ObservationStore(Path(args.observations), A, create=True)
            print(json.dumps({"observations_created": True, **A.context}))
        elif args.command == "show-observations":
            rows, records = ObservationStore(Path(args.observations), A).read()
            print(json.dumps({"phases": [r["phase"] for r in rows], "receipts": records}))
        elif args.command == "observe":
            store = ObservationStore(Path(args.observations), A)
            row = file_row(args.receipt, args.receipt_sha256)
            data = admit_native(A, args.phase, row, store=store)
            flow = Workflow(A, store, signature_runner=subprocess.run)
            if args.phase == "INSTALLED":
                flow.initial_install(data["kos_install_receipt"], data, doctor_runner=subprocess.run)
            elif args.phase == "ARMED":
                flow.arm(data["armed_receipt"])
            elif args.phase == "REBOOT_VERIFIED":
                flow.recovered(data["recovery_receipt"])
            else:
                data["admitted_command_inputs"] = A.B["phase_command_inputs"].get(args.phase, {})
                store.append(args.phase, data)
            print(json.dumps({"phase_observed": args.phase, **A.context}))
        else:
            validate_release(A)
            store = ObservationStore(Path(args.observations), A)
            actual = {k: os.environ.get(k) for k in ("cto_payload", "cto_runtime", "cto_runtime_sha")}
            execute(A, store, args.role, actual, invocation_directory=args.invocations)
            # Never echo a role's captured stdout/stderr into a public CLI receipt.
            print(json.dumps({"checked_role_completed": args.role, **A.context}))
        return 0
    except Refusal as error:
        print(json.dumps({"refused": error.code, "allocation_performed": False}), file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError, TypeError) as error:
        # Do not leak raw data or private output from failed input/target parsing.
        print(json.dumps({"refused": "input_or_adapter_error", "allocation_performed": False}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
