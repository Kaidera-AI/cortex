"""Bounded v1 export download, validation and recoverable two-path publication.

The checksum is replaced FIRST: interruption leaves either the old valid pair,
a detectable mismatch, or the complete new pair. This is not a crash-atomic pair.
Uncertain publication retains the private staging directory for operator recovery.
No promise is made against a hostile writer running under the same account.
"""
import argparse
from datetime import datetime, timezone
from decimal import Decimal, DecimalException
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import shutil
import stat
import subprocess
import sys
import tempfile
from urllib.parse import quote, urlencode


MAX_BYTES = 256 * 1024 * 1024
MAX_VALUE = 1024 * 1024
MAX_TABLES = 4096
CHUNK = 65536
FORMAT = "kaidera.cortex-project.v1"
NAME = re.compile(r"[a-z_][a-z0-9_]*\Z")


def reject():
    raise ValueError("invalid export")


def object_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            reject()
        result[key] = value
    return result


class Reader:
    """Only one scalar/metadata object/row is decoded at once (at most 1 MiB)."""
    def __init__(self, stream):
        self.stream, self.text, self.end = stream, "", False
        self.decoder = json.JSONDecoder(object_pairs_hook=object_pairs,
                                        parse_float=Decimal,
                                        parse_constant=lambda _: reject())

    def fill(self):
        part = self.stream.read(CHUNK)
        self.end = not part
        self.text += part

    def whitespace(self):
        self.text = self.text.lstrip(" \t\r\n")
        while not self.text and not self.end:
            self.fill()
            self.text = self.text.lstrip(" \t\r\n")

    def take(self, token):
        self.whitespace()
        if not self.text.startswith(token):
            reject()
        self.text = self.text[len(token):]

    def members(self, opening, closing):
        self.take(opening)
        self.whitespace()
        if self.text.startswith(closing):
            self.take(closing)
            return
        while True:
            yield
            self.whitespace()
            if self.text.startswith(closing):
                self.take(closing)
                return
            self.take(",")

    def value(self):
        self.whitespace()
        while True:
            try:
                value, end = self.decoder.raw_decode(self.text)
                # raw_decode accepts a numeric prefix (e.g. "12" in "12e").
                # Wait for its delimiter, but never accumulate an unbounded token.
                numeric_tail = (type(value) in (int, Decimal) and end < len(self.text)
                                and self.text[end] in ".eE+-0123456789")
                if (end == len(self.text) or numeric_tail) and not self.end:
                    if len(self.text.encode("utf-8")) > MAX_VALUE:
                        reject()
                    self.fill()
                    continue
                if end < len(self.text) and self.text[end] not in " \t\r\n,:]}":
                    reject()
                if len(self.text[:end].encode("utf-8")) > MAX_VALUE:
                    reject()
                self.text = self.text[end:]
                return value
            except json.JSONDecodeError:
                if self.end or len(self.text.encode("utf-8")) > MAX_VALUE:
                    reject()
                self.fill()

    def key(self, seen):
        key = self.value()
        if not isinstance(key, str) or key in seen:
            reject()
        seen.add(key)
        self.take(":")
        return key


def validate(stream, project, requested):
    reader = Reader(stream)
    fields, table_names, rows = {}, set(), 0
    seen = set()
    for _ in reader.members("{", "}"):
        key = reader.key(seen)
        if key not in {"format", "exported_at", "source_project", "table_count", "row_count", "tables", "selection"}:
            reject()
        if key != "tables":
            fields[key] = reader.value()
            continue
        for _ in reader.members("[", "]"):
            table, table_seen, table_rows = {}, set(), 0
            for _ in reader.members("{", "}"):
                field = reader.key(table_seen)
                if field not in {"schema_name", "table_name", "rows"}:
                    reject()
                if field != "rows":
                    table[field] = reader.value()
                    continue
                for _ in reader.members("[", "]"):
                    if not isinstance(reader.value(), dict):
                        reject()
                    table_rows += 1
            schema, name = table.get("schema_name"), table.get("table_name")
            if schema not in {"public", "cortex"} or not isinstance(name, str) or not NAME.fullmatch(name) or "rows" not in table_seen:
                reject()
            qualified = f"{schema}.{name}"
            if qualified in table_names or len(table_names) >= MAX_TABLES:
                reject()
            table_names.add(qualified)
            rows += table_rows
    reader.whitespace()
    source = fields.get("source_project")
    if reader.text or not reader.end or "tables" not in seen or fields.get("format") != FORMAT or not isinstance(source, dict) or source.get("project_key") != project:
        reject()
    if type(fields.get("row_count")) is not int or fields["row_count"] != rows or type(fields.get("table_count")) is not int or fields["table_count"] != len(table_names):
        reject()
    selection = fields.get("selection")
    if selection is not None:
        if not isinstance(selection, dict) or set(selection) != {"mode", "tables"} or selection["mode"] not in {"all", "tables"}:
            reject()
        selected = selection["tables"]
        if not isinstance(selected, list) or any(not isinstance(x, str) for x in selected) or sorted(set(selected)) != selected or not table_names.issubset(selected):
            reject()
        if any(not re.fullmatch(r"(?:public|cortex)\.[a-z_][a-z0-9_]*", value) for value in selected) or len(selected) > MAX_TABLES or (not requested and selection["mode"] != "all"):
            reject()
    if requested and (not selection or selection != {"mode": "tables", "tables": sorted(requested)}):
        reject()
    return rows


def identity(info):
    return info.st_dev, info.st_ino


def record(fd, name):
    try:
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid() or info.st_size > MAX_BYTES:
        reject()
    return (*identity(info), info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def parent_fd(path):
    if ".." in path.parts:
        reject()
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def check_parent(path, fd):
    current = parent_fd(path)
    try:
        if identity(os.fstat(current)) != identity(os.fstat(fd)):
            reject()
    finally:
        os.close(current)


def download(command, env):
    # The new session belongs only to this finite helper and its curl process.
    # A Python timeout must not leave curl writing after staging is cleaned up.
    with subprocess.Popen(command, env=env, start_new_session=True,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as child:
        try:
            result = child.wait(timeout=190)
        except BaseException:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait(timeout=5)
            raise
        if result:
            raise ValueError("export download failed")


def export(project, output, tables):
    path = Path(os.path.abspath(output))
    if ".." in Path(output).parts or any(ord(c) < 32 or c == "\\" for c in str(output)) or path.name in {"", ".", ".."}:
        reject()
    parent = parent_fd(path.parent)
    stage = None
    temp_fd = None
    keep = False
    owned = {}
    try:
        names = [path.name + ".sha256", path.name]
        previous = {name: record(parent, name) for name in names}
        stage = Path(tempfile.mkdtemp(prefix=".cortex-export-", dir=path.parent))
        temp_fd = os.open(stage, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        stage_id = identity(os.fstat(temp_fd))
        for name in ("payload", "checksum"):
            fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=temp_fd)
            owned[name] = identity(os.fstat(fd))
            os.close(fd)
        env = dict(os.environ, CORTEX_PROJECT=project)
        agent = env.get("CORTEX_AGENT") or env.get("CORTEX_AGENT_ID") or env.get("BEAT_CORTEX_AGENT", "")
        endpoint = "/admin/projects/" + quote(project, safe="") + "/export"
        if tables:
            endpoint += "?" + urlencode({"tables": ",".join(tables)})
        helper = Path(__file__).with_name("_cortex_api.sh")
        command = ['/bin/bash', '-c', 'source "$1"; cortex_api_download_admin "$2" "$3" "$4"',
                   'cortex-export', str(helper), endpoint, str(stage / "payload"), agent]
        download(command, env)
        check_parent(path.parent, parent)
        if identity(os.stat(stage, follow_symlinks=False)) != stage_id:
            reject()
        payload_record = record(temp_fd, "payload")
        if payload_record is None or payload_record[:2] != owned["payload"]:
            reject()
        fd = os.open("payload", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=temp_fd)
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            count = validate(stream, project, tables)
        digest = hashlib.sha256()
        fd = os.open("payload", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=temp_fd)
        with os.fdopen(fd, "rb") as stream:
            while chunk := stream.read(CHUNK):
                digest.update(chunk)
            os.fsync(stream.fileno())
        if record(temp_fd, "payload") != payload_record:
            reject()
        fd = os.open("checksum", os.O_WRONLY | os.O_NOFOLLOW, dir_fd=temp_fd)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(f"{digest.hexdigest()}  {path.name}\n")
            stream.flush()
            os.fsync(stream.fileno())
        for index, name in enumerate(names):
            if previous[name] is not None:
                source = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
                target = os.open(f"backup{index}", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=temp_fd)
                owned[f"backup{index}"] = identity(os.fstat(target))
                with os.fdopen(source, "rb") as reader, os.fdopen(target, "wb") as writer:
                    shutil.copyfileobj(reader, writer, CHUNK)
                    writer.flush()
                    os.fchmod(writer.fileno(), stat.S_IMODE(previous[name][2]))
                    os.fsync(writer.fileno())
        check_parent(path.parent, parent)
        if any(record(parent, name) != previous[name] for name in names):
            reject()
        published = {}
        attempted = {}
        keep = True
        try:
            for name, staged in zip(names, ("checksum", "payload")):
                check_parent(path.parent, parent)
                if record(parent, name) != previous[name]:
                    reject()
                # Record the owned source BEFORE the mutation. A post-rename
                # stat can fail even though rename itself already succeeded.
                attempted[name] = record(temp_fd, staged)
                if attempted[name] is None or attempted[name][:2] != owned[staged]:
                    reject()
                os.replace(staged, name, src_dir_fd=temp_fd, dst_dir_fd=parent)
                published[name] = record(parent, name)
                os.fsync(parent)
            check_parent(path.parent, parent)
            if any(record(parent, name) != published[name] for name in names):
                reject()
        except Exception:
            for index, name in reversed(list(enumerate(names))):
                if name not in attempted:
                    continue
                check_parent(path.parent, parent)
                current = record(parent, name)
                if current == previous[name]:
                    continue
                # Rename may update ctime. Identity/mode/size/mtime must match
                # the owned source; never overwrite a different replacement.
                if current is None or current[:5] != attempted[name][:5]:
                    raise RuntimeError("export publication uncertain; staging retained") from None
                if previous[name] is None:
                    os.unlink(name, dir_fd=parent)
                else:
                    os.replace(f"backup{index}", name, src_dir_fd=temp_fd, dst_dir_fd=parent)
                os.fsync(parent)
            keep = False
            raise
        keep = False
        print(f"Exported {count} rows. SHA-256: {digest.hexdigest()}")
    finally:
        try:
            if temp_fd is not None and not keep:
                for name, expected in owned.items():
                    try:
                        if identity(os.stat(name, dir_fd=temp_fd, follow_symlinks=False)) == expected:
                            os.unlink(name, dir_fd=temp_fd)
                    except FileNotFoundError:
                        pass
                if identity(os.stat(stage, follow_symlinks=False)) == stage_id:
                    os.rmdir(stage)
        finally:
            if temp_fd is not None:
                os.close(temp_fd)
            os.close(parent)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Export a Cortex v1 project; import requires a registered target. Root metadata is retained, not installed.")
    parser.add_argument("project")
    parser.add_argument("--output")
    parser.add_argument("--tables")
    args = parser.parse_args(argv)
    try:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", args.project):
            reject()
        selected = args.tables.split(",") if args.tables is not None else []
        if len(selected) != len(set(selected)) or any(not re.fullmatch(r"(?:public|cortex)\.[a-z_][a-z0-9_]*", value) for value in selected):
            reject()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        export(args.project, args.output or f"cortex-project-{args.project}-{stamp}.json", selected)
        return 0
    except (OSError, ValueError, RuntimeError, RecursionError, DecimalException, subprocess.SubprocessError):
        print("ERROR: Cortex export failed; no successful export was published. Retain any .cortex-export-* staging for recovery.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
