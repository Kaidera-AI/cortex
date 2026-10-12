"""Repository product CLI: shared admission, raw verification and deterministic export.

Native engine execution is a separately gated R02b operation. No host SDK lives here.
"""

import argparse
import json
from pathlib import Path
from . import compare, export


def main(argv=None):
    parser = argparse.ArgumentParser(prog="cortex-reproducibility")
    commands = parser.add_subparsers(dest="command", required=True)
    raw = commands.add_parser("compare")
    raw.add_argument("--a", type=Path, required=True)
    raw.add_argument("--b", type=Path, required=True)
    write = commands.add_parser("export")
    write.add_argument("--layout", type=Path, required=True)
    write.add_argument("--output", type=Path, required=True)
    write.add_argument("--epoch", type=int, required=True)
    prepare = commands.add_parser("prepare")
    for flag in (
        "input",
        "input-sha256",
        "input-root",
        "policy",
        "policy-record-sha256",
        "output",
    ):
        prepare.add_argument("--" + flag, required=True)
    prepare.add_argument(
        "--profile-v1",
        action="store_true",
        help="require the owner-admitted descriptor profile-v1 before cloning",
    )
    commands.add_parser("contract")
    args = parser.parse_args(argv)
    try:
        if args.command == "compare":
            compare.compare_archives(args.a, args.b)
            result = {
                "result": "PASS",
                "qualification_scope": "fixture",
                "whole_archive_equal": True,
                "native_build_qualified": False,
            }
        elif args.command == "export":
            result = export.write_archive(args.layout, args.output, args.epoch)
            result.update(
                result="PASS",
                qualification_scope="fixture",
                native_build_qualified=False,
            )
        elif args.command == "prepare":
            from cortex_core.cli.release import _document
            from cortex_core.release.codec import decode_json
            from cortex_core.release.inputs import parse_build_input
            from .inputs import FilesystemReader, prepare

            raw_input = _document(args.input, args.input_sha256, 1024 * 1024)
            policy = decode_json(
                _document(args.policy, args.policy_record_sha256, 1024 * 1024),
                1024 * 1024,
            )
            if not isinstance(policy, dict):
                raise ValueError("independently supplied policy object required")
            if args.profile_v1:
                policy = dict(policy, require_profile_v1=True)
            parsed = parse_build_input(raw_input)
            reader = FilesystemReader(
                args.input_root, max_bytes=parsed["resources"]["memory_bytes"]
            )
            admitted, clone = prepare(raw_input, reader, policy, args.output)
            result = {
                "result": "PREPARED",
                "input_sha256": admitted.input_sha256,
                "qualification_scope": admitted.document["admission_policy"][
                    "qualification_scope"
                ],
                "clone": str(clone),
                "profile_v1_required": args.profile_v1
                or admitted.document["admission_policy"]["qualification_scope"]
                == "native",
                "native_build_qualified": False,
            }
        else:
            # Import only the real owner implementation; never invent its parser.
            from cortex_core.release.inputs import parse_build_input, admit_build_input
            from cortex_core.release.receipts import (
                parse_build_receipt,
                verify_build_receipt,
            )

            if not all(
                callable(f)
                for f in [
                    parse_build_input,
                    admit_build_input,
                    parse_build_receipt,
                    verify_build_receipt,
                ]
            ):
                raise ValueError("shared R01 callable unavailable")
            result = {
                "result": "READY",
                "schemas": ["cortex.build-input.v1", "cortex.build-receipt.v1"],
                "native_build_qualified": False,
            }
    except (ValueError, OSError, ModuleNotFoundError) as error:
        print(
            json.dumps(
                {
                    "result": "BLOCKED"
                    if isinstance(error, ModuleNotFoundError)
                    else "RED",
                    "reason": type(error).__name__,
                    "native_build_qualified": False,
                }
            )
        )
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
