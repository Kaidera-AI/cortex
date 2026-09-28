"""Run an isolated restored-copy conversion through private Unix sockets only.

Usage: python -m cortex_v2.converter_cli --private-root DIR --source-dsn-file FILE
       --target-dsn-file FILE --policy-file FILE --snapshot-sha256 HEX
       --revision CONVERTER_COMMIT
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import stat
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import asyncpg

from .converter import ConversionPolicy, validate_snapshot_sha
from .converter_run import convert_snapshot


def _private_file(path: Path, private_root: Path) -> bytes:
    if path.is_symlink() or not path.resolve(strict=True).is_relative_to(private_root):
        raise ValueError("converter input must be a private regular file")
    handle = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(handle)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise ValueError("converter input must be owned and mode 0600")
        with os.fdopen(handle, "rb", closefd=False) as stream:
            return stream.read()
    finally:
        os.close(handle)


def _socket_dsn(raw: bytes, database: str, private_root: Path) -> str:
    dsn = raw.decode("utf-8").strip()
    parsed = urlsplit(dsn)
    params = parse_qs(parsed.query, strict_parsing=True)
    if (
        parsed.scheme not in {"postgresql", "postgres"}
        or parsed.netloc
        or parsed.path != f"/{database}"
        or set(params) != {"host", "port", "user"}
        or any(len(values) != 1 for values in params.values())
        or not params["port"][0].isdigit()
        or not params["user"][0]
    ):
        raise ValueError("converter DSNs require separate named local databases")
    socket = Path(params["host"][0]).resolve(strict=True)
    if not socket.is_dir() or not socket.is_relative_to(private_root):
        raise ValueError("converter DSN socket must reside in private image")
    return dsn


async def _run(args: argparse.Namespace) -> dict[str, str | int]:
    private_root = args.private_root.resolve(strict=True)
    info = private_root.stat()
    if (
        not private_root.is_dir()
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        raise ValueError("private image working directory must be owned and mode 0700")
    policy = ConversionPolicy.from_bytes(
        _private_file(args.policy_file, private_root)
    )
    source_dsn = _socket_dsn(
        _private_file(args.source_dsn_file, private_root),
        "legacy_restore", private_root,
    )
    target_dsn = _socket_dsn(
        _private_file(args.target_dsn_file, private_root),
        "cortex_v2", private_root,
    )
    if source_dsn == target_dsn:
        raise ValueError("source and target DSNs must differ")
    source = await asyncpg.connect(source_dsn)
    try:
        target = await asyncpg.connect(target_dsn)
        try:
            source_role = await source.fetchrow(
                """SELECT current_user AS role, inet_server_addr() AS tcp,
                          rolsuper, rolcreatedb, rolcreaterole, rolreplication
                     FROM pg_roles WHERE rolname=current_user"""
            )
            target_role = await target.fetchrow(
                """SELECT current_user AS role, inet_server_addr() AS tcp,
                          rolsuper FROM pg_roles WHERE rolname=current_user"""
            )
            source_writable = await source.fetchval(
                """SELECT has_database_privilege(
                           current_user, current_database(), 'CREATE'
                       )
                       OR EXISTS (
                           SELECT 1 FROM pg_namespace
                            WHERE nspname IN ('public','cortex','cortex_auth')
                              AND has_schema_privilege(
                                  current_user, oid, 'CREATE'
                              )
                       )
                       OR EXISTS (
                           SELECT 1 FROM pg_class c
                           JOIN pg_namespace n ON n.oid=c.relnamespace
                            WHERE n.nspname IN ('public','cortex','cortex_auth')
                              AND c.relkind='r'
                              AND (
                                  has_table_privilege(current_user,c.oid,'INSERT')
                                  OR has_table_privilege(current_user,c.oid,'UPDATE')
                                  OR has_table_privilege(current_user,c.oid,'DELETE')
                                  OR has_table_privilege(current_user,c.oid,'TRUNCATE')
                              )
                       )"""
            )
            if (
                source_role["tcp"] is not None
                or target_role["tcp"] is not None
                or source_role["role"] in {"postgres", "cortex_v2_migrator"}
                or any(source_role[field] for field in (
                    "rolsuper", "rolcreatedb", "rolcreaterole", "rolreplication"
                ))
                or target_role["role"] != "cortex_v2_migrator"
                or target_role["rolsuper"]
                or source_writable
            ):
                raise ValueError(
                    "converter requires private non-owner source read role"
                )
            result = await convert_snapshot(
                source, target, policy,
                snapshot_sha256=validate_snapshot_sha(args.snapshot_sha256),
                converter_revision=args.revision,
            )
            return {
                "run_id": str(result.run_id),
                "source_count": result.source_count,
                "target_count": result.target_count,
                "quarantined_count": result.quarantined_count,
                "skipped_count": result.skipped_count,
                "policy_sha256": policy.sha256,
            }
        finally:
            await target.close()
    finally:
        await source.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-root", type=Path, required=True)
    parser.add_argument("--source-dsn-file", type=Path, required=True)
    parser.add_argument("--target-dsn-file", type=Path, required=True)
    parser.add_argument("--policy-file", type=Path, required=True)
    parser.add_argument("--snapshot-sha256", required=True)
    parser.add_argument("--revision", required=True)
    args = parser.parse_args(argv)
    print(json.dumps(asyncio.run(_run(args)), sort_keys=True))


if __name__ == "__main__":
    main()
