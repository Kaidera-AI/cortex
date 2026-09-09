#!/usr/bin/env python3
"""Package verified projected bytes as a deterministic Cortex source release."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import posixpath
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tarfile
from urllib.parse import urlsplit

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import image_manifest as images


MAX_BYTES = 256 * 1024 * 1024
MAX_MEMBERS = 20000
COMPONENTS = {name: f"packages/{name}" for name in
              ("api", "schema", "cli", "containers", "deploy", "installer")}
ENTRY_POINTS = {
    "api_main": "packages/api/main.py",
    "schema": "packages/schema/schema.sql",
    "compose": "packages/deploy/docker-compose.yml",
    "db_containerignore": "packages/.containerignore",
    "db_dockerignore": "packages/.dockerignore",
}
FORBIDDEN_PARTS = {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache"}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_path(path: Path, *, missing_leaf: bool = False) -> Path:
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("paths must be absolute without parent traversal")
    candidate = Path(path.anchor)
    for index, part in enumerate(path.parts[1:], start=1):
        candidate /= part
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            if missing_leaf and index == len(path.parts) - 1:
                return path
            raise ValueError("path parent is unavailable") from None
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError("symlink paths are forbidden")
        if index < len(path.parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("path ancestry must be directories")
    return path


def regular_bytes(path: Path, limit: int) -> tuple[bytes, int]:
    safe_path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        metadata = os.fstat(fd)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
                or metadata.st_size > limit or metadata.st_mode & 0o7000):
            raise ValueError("projection contains an unsafe file or exceeds the size limit")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            data = handle.read(limit + 1)
        if len(data) > limit:
            raise ValueError("projection exceeds the size limit")
        after = os.fstat(fd)
        if (metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError("projection changed during snapshot")
        return data, 0o755 if metadata.st_mode & 0o111 else 0o644
    finally:
        os.close(fd)


def decode_json(data: bytes, name: str) -> dict:
    try:
        value = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        raise ValueError(f"{name} is not valid JSON") from None
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def snapshot(projection: Path) -> tuple[dict, dict[str, tuple[bytes, int]], set[str]]:
    projection = safe_path(projection)
    if not projection.is_dir():
        raise ValueError("projection must be a directory")
    manifest_bytes, manifest_mode = regular_bytes(projection / "PROJECTION_MANIFEST.json", 256 * 1024)
    manifest = decode_json(manifest_bytes, "projection manifest")
    if (manifest.get("schema") != "cortex.projection_manifest.v1"
            or manifest.get("source_repo") != "Kaidera-AI/kaideraos"
            or manifest.get("source_provenance") != "pristine git archive of committed HEAD"
            or not re.fullmatch(r"[a-f0-9]{40}", str(manifest.get("source_revision", "")))):
        raise ValueError("projection provenance is invalid")
    if (not isinstance(manifest.get("components"), dict)
            or set(manifest["components"]) != set(COMPONENTS)
            or not isinstance(manifest.get("entry_points"), dict)
            or set(manifest["entry_points"]) != set(ENTRY_POINTS)):
        raise ValueError("projection manifest must cover the complete standalone package")

    files = {"PROJECTION_MANIFEST.json": (manifest_bytes, manifest_mode)}
    directories: set[str] = set()
    total = len(manifest_bytes)
    packages = safe_path(projection / "packages")
    if not packages.is_dir():
        raise ValueError("projection has no packages directory")
    for current, dirs, names in os.walk(packages, followlinks=False):
        dirs.sort()
        names.sort()
        relative = Path(current).relative_to(projection).as_posix()
        directories.add(relative)
        if len(files) + len(directories) > MAX_MEMBERS:
            raise ValueError("projection has too many archive members")
        for name in [*dirs, *names]:
            target = Path(current) / name
            member = target.relative_to(projection).as_posix()
            parts = PurePosixPath(member).parts
            if (set(parts) & FORBIDDEN_PARTS or name == ".env"
                    or name.startswith(".env.") or name.endswith(".env")
                    or name.endswith((".pyc", ".pyo", ".key", ".pem"))
                    or "\\" in member or re.search(r"[\x00-\x1f\x7f]", member)):
                raise ValueError("projection contains unsafe or generated private/cache material")
            metadata = target.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not (
                    stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)):
                raise ValueError("projection links and special files are forbidden")
            if stat.S_ISDIR(metadata.st_mode):
                if metadata.st_mode & 0o7000:
                    raise ValueError("projection directory has unsafe special permission bits")
                continue
            data, mode = regular_bytes(target, MAX_BYTES - total)
            total += len(data)
            files[member] = (data, mode)
            if len(files) + len(directories) > MAX_MEMBERS:
                raise ValueError("projection has too many archive members")

    covered = {"PROJECTION_MANIFEST.json"}
    for name, prefix in COMPONENTS.items():
        record = manifest["components"][name]
        if not isinstance(record, dict) or record.get("path") != prefix or prefix not in directories:
            raise ValueError("projection component path is invalid")
        members = {member: item for member, item in files.items() if member.startswith(prefix + "/")}
        # Match the canonical projector exactly: sorted directories, then sorted
        # filenames within each directory (not one global full-path sort).
        ordered = sorted(members, key=lambda item: (str(PurePosixPath(item).parent), PurePosixPath(item).name))
        digest = hashlib.sha256()
        for member in ordered:
            digest.update(member[len(prefix) + 1:].encode("utf-8"))
            digest.update(members[member][0])
        if record.get("files") != len(members) or record.get("tree_sha256") != digest.hexdigest():
            raise ValueError(f"projection component verification failed: {name}")
        covered.update(members)
    for name, member in ENTRY_POINTS.items():
        if member not in files or sha(files[member][0]) != manifest["entry_points"][name]:
            raise ValueError(f"projection entry-point verification failed: {name}")
        covered.add(member)
    if set(files) != covered:
        raise ValueError("package files are not covered by projection provenance")
    if any(directory != "packages" and not any(
            directory == prefix or directory.startswith(prefix + "/")
            for prefix in COMPONENTS.values()) for directory in directories):
        raise ValueError("package directories are not covered by projection provenance")

    identity = decode_json(files.get("packages/deploy/release.json", (b"", 0))[0], "payload identity")
    if (identity.get("schema") != "cortex.payload.v1"
            or identity.get("source_revision") != manifest["source_revision"]
            or not re.fullmatch(r"\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?", str(identity.get("version", "")))
            or not re.fullmatch(r"[a-f0-9]{64}", str(identity.get("schema_revision", "")))):
        raise ValueError("inner payload identity disagrees with projection provenance")
    migration_prefix = "packages/schema/migrations/"
    migrations = sorted((member[len(migration_prefix):], sha(data))
                        for member, (data, _mode) in files.items()
                        if member.startswith(migration_prefix)
                        and "/" not in member[len(migration_prefix):] and member.endswith(".sql"))
    if not migrations or sha("".join(f"{name}\t{checksum}\n" for name, checksum in migrations).encode()) != identity["schema_revision"]:
        raise ValueError("payload schema revision disagrees with the migration inventory")
    runtime = files.get("packages/deploy/cortex-runtime")
    if runtime is None or runtime[1] != 0o755:
        raise ValueError("payload runtime must be executable")
    return identity, files, directories


def archive_bytes(files: dict, directories: set[str]) -> bytes:
    tar_buffer = io.BytesIO()
    with tarfile.open(fileobj=tar_buffer, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name in sorted(set(files) | directories):
            info = tarfile.TarInfo(name)
            info.uid = info.gid = info.mtime = 0
            info.uname = info.gname = ""
            if name in directories:
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                archive.addfile(info)
            else:
                data, info.mode = files[name]
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
    if tar_buffer.tell() > MAX_BYTES:
        raise ValueError("archive exceeds the installer's expanded size limit")
    output = io.BytesIO()
    with gzip.GzipFile(filename="", fileobj=output, mode="wb", mtime=0, compresslevel=9) as stream:
        stream.write(tar_buffer.getvalue())
    if output.tell() > MAX_BYTES:
        raise ValueError("archive exceeds the installer's compressed size limit")
    return output.getvalue()


def validate_release_url(url: str) -> str:
    if len(url) > 8192 or re.search(r"[\x00-\x20\x7f]", url):
        raise ValueError("release URL is invalid")
    value = urlsplit(url)
    if value.scheme != "https" or not value.hostname or value.username or value.password or value.fragment:
        raise ValueError("--url requires an absolute HTTPS URL without credentials or fragments")
    return url


def installation_files(identity, files, directories, inventory, platform):
    """Derive new files only; the independently validated projection is immutable."""
    inventory = images.validate_inventory(inventory, identity)
    if platform not in inventory['platforms']:
        raise ValueError('selected platform has no complete image locks')
    try:
        import yaml  # Maintainer-only renderer; never imported by cortex-runtime.
    except ImportError:
        raise ValueError('prebuilt packaging requires maintainer PyYAML 6.0.3') from None
    try:
        compose = yaml.safe_load(files['packages/deploy/docker-compose.yml'][0])
    except yaml.YAMLError:
        raise ValueError('source Compose is invalid YAML') from None
    if not isinstance(compose, dict) or set(compose.get('services', {})) != set(images.SERVICE_ROLES):
        raise ValueError('source Compose must cover exactly the runtime services')
    compose = {key: value for key, value in compose.items() if not key.startswith('x-')}
    for service, role in images.SERVICE_ROLES.items():
        config = compose['services'][service]
        config.pop('build', None)
        config.update(image=images.image_ref(inventory['platforms'][platform][role]),
                      platform=platform, pull_policy='never')
        for volume in config.get('volumes', []):
            if not isinstance(volume, str):
                raise ValueError('source Compose requires reviewed short-form volume bindings')
            source = volume.split(':', 1)[0]
            if source.startswith('.'):
                member = posixpath.normpath('packages/deploy/' + source)
                if not member.startswith('packages/') or member not in files and member not in directories:
                    raise ValueError('installation Compose references an unshipped source input')
    images.validate_compose(compose, inventory, platform)
    derived = {images.COMPOSE_FILE: (images.encode(compose), 0o644),
               images.LOCK_FILE: (images.encode(inventory), 0o644)}
    if set(derived) & set(files) or images.INSTALL_FILE in files:
        raise ValueError('projection already contains generated installation files')
    install = {**identity, 'schema': 'cortex.install.v1', 'delivery_kind': 'prebuilt',
               'platform': platform, 'files': {name: sha(body) for name, (body, _) in {**files, **derived}.items()}}
    required = {'packages/deploy/image_manifest.py'}
    if not required <= set(files):
        raise ValueError('projection is missing the prebuilt runtime image helper')
    derived[images.INSTALL_FILE] = (images.encode(install), 0o644)
    return derived


def build_release(projection: Path, output: Path, compatible=(), url: str | None = None,
                  *, inventory=None, platform=None) -> dict:
    projection = safe_path(projection)
    output = safe_path(output, missing_leaf=True)
    if (output == Path(output.anchor) or output == Path.home()
            or output == projection or projection in output.parents):
        raise ValueError("output must be a dedicated directory outside the projection")
    parent = output if output.exists() else output.parent
    metadata = parent.lstat()
    if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o022):
        raise ValueError("output directory must have safe ownership and permissions")
    if output.exists() and (stat.S_IMODE(metadata.st_mode) != 0o700 or any(output.iterdir())):
        raise ValueError("output must be an empty owner-private directory; overwrites are forbidden")
    compatible = list(compatible)
    if any(not re.fullmatch(r"[a-f0-9]{64}", item) for item in compatible):
        raise ValueError("compatible schemas must be exact migration inventory SHA-256 values")
    if url is not None:
        url = validate_release_url(url)
    identity, files, directories = snapshot(projection)
    if (inventory is None) != (platform is None):
        raise ValueError('prebuilt packaging requires both --images and --platform')
    derived = installation_files(identity, files, directories, inventory, platform) if inventory is not None else {}
    files = {**files, **derived}
    data = archive_bytes(files, directories)
    stem = f"cortex-{identity['version']}-{identity['source_revision'][:12]}"
    if derived:
        stem += '-prebuilt-' + platform.replace('/', '-')
    archive_path, manifest_path = output / f"{stem}.tar.gz", output / f"{stem}.json"
    release = {"schema": "cortex.release.v1", "version": identity["version"],
               "source_revision": identity["source_revision"], "schema_revision": identity["schema_revision"],
               "compatible_schema_revisions": sorted(set([identity["schema_revision"], *compatible])),
               "url": url or archive_path.as_uri(), "sha256": sha(data)}
    if derived:
        release.update(delivery_kind='prebuilt', platform=platform,
                       installation_sha256=sha(derived[images.INSTALL_FILE][0]))
    if not output.exists():
        output.mkdir(mode=0o700)
    for target, body in [(archive_path, data), (manifest_path, (json.dumps(release, indent=2, sort_keys=True) + "\n").encode())]:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
    fd = os.open(output, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return {"archive": str(archive_path), "manifest": str(manifest_path), "sha256": release["sha256"],
            "version": identity["version"], "source_revision": identity["source_revision"],
            "schema_revision": identity["schema_revision"]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--projection", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--compatible-schema", action="append", default=[])
    parser.add_argument("--url", help="final HTTPS archive URL; default is a local qualification file URL")
    parser.add_argument('--images', type=Path, help='maintainer-provided cortex.images.v1 platform manifest inventory')
    parser.add_argument('--platform', choices=sorted(images.PLATFORMS), help='derive a prebuilt-only install payload for this Linux backend')
    args = parser.parse_args(argv)
    try:
        inventory = decode_json(regular_bytes(args.images, 256 * 1024)[0], 'image inventory') if args.images else None
        print(json.dumps(build_release(args.projection, args.output_dir, args.compatible_schema, args.url,
                                      inventory=inventory, platform=args.platform), sort_keys=True))
    except (OSError, ValueError, tarfile.TarError) as error:
        print(f"package release refused: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
