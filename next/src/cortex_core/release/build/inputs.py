"""Direct R01 consumer, with no private schema or field translation."""


def admit(raw, input_reader, accepted_policy):
    from cortex_core.release.inputs import parse_build_input, admit_build_input

    return admit_build_input(parse_build_input(raw), input_reader, accepted_policy)


class FilesystemReader:
    """Bounded physical observations for the real R01 admission API."""

    def __init__(self, root, *, max_bytes):
        from pathlib import Path

        root = Path(root)
        if (
            root.is_symlink()
            or not root.is_dir()
            or type(max_bytes) is not int
            or max_bytes <= 0
        ):
            raise ValueError("physical input root and explicit byte budget required")
        self.root = root.resolve()
        self.max_bytes = max_bytes
        self.measured_bytes = 0
        self.captured = {}

    @staticmethod
    def identity(value):
        return (
            value.st_dev,
            value.st_ino,
            value.st_size,
            value.st_mode,
            value.st_uid,
            value.st_gid,
            value.st_mtime_ns,
            value.st_ctime_ns,
        )

    def path(self, name):
        from cortex_core.release.codec import relative_path

        path = self.root / relative_path(name)
        for parent in path.parents:
            if parent == self.root:
                break
            if parent.is_symlink():
                raise ValueError("input parent link refused")
        return path

    def __call__(self, name):
        import os
        import stat
        from cortex_core.release.inputs import FileObservation

        path = self.path(name)
        before = path.stat(follow_symlinks=False)
        identity = self.identity(before)
        if name in self.captured:
            observation, captured = self.captured[name]
            if captured != identity:
                raise ValueError("captured input changed")
            return observation
        if before.st_size + self.measured_bytes > self.max_bytes:
            raise ValueError("input byte budget exceeded")
        if stat.S_ISREG(before.st_mode):
            with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
                if self.identity(os.fstat(stream.fileno())) != identity:
                    raise ValueError("input changed before read")
                raw = stream.read(self.max_bytes - self.measured_bytes + 1)
                if self.identity(os.fstat(stream.fileno())) != identity:
                    raise ValueError("input changed during read")
            kind, target = "regular", None
        elif stat.S_ISLNK(before.st_mode):
            target = os.readlink(path)
            raw, kind = target.encode("utf-8"), "symlink"
        else:
            raise ValueError("unsupported input member")
        if (
            self.identity(self.path(name).stat(follow_symlinks=False)) != identity
            or len(raw) + self.measured_bytes > self.max_bytes
        ):
            raise ValueError("input changed or exceeds budget")
        observation = FileObservation(raw, kind, stat.S_IMODE(before.st_mode), target)
        self.captured[name] = observation, identity
        self.measured_bytes += len(raw)
        return observation

    def inventory(self, context_root):
        root = self.path(context_root)
        if root.is_symlink() or not root.is_dir():
            raise ValueError("physical source context required")
        return sorted(
            path.relative_to(self.root).as_posix()
            for path in root.rglob("*")
            if not path.is_dir() or path.is_symlink()
        )

    def assert_unchanged(self):
        for name, (_, expected) in self.captured.items():
            if self.identity(self.path(name).stat(follow_symlinks=False)) != expected:
                raise ValueError("source changed after admission")


def require_verified(value):
    from cortex_core.release.contracts import VerifiedBuildInput, _SEAL

    if not isinstance(value, VerifiedBuildInput) or value._seal is not _SEAL:
        raise ValueError("real R01 verified input required")
    return value


def materialize(verified, reader, destination):
    """Copy only admitted captured buffers into a new owned input clone."""
    import os
    from pathlib import Path
    from cortex_core.release.codec import sha256

    require_verified(verified)
    if not isinstance(reader, FilesystemReader):
        raise ValueError("physical captured input reader required")
    names = {row["path"] for row in verified.inventory}
    if names != set(reader.captured):
        raise ValueError("captured input inventory incomplete or extra")
    reader.assert_unchanged()
    context = verified.document["source"]["context_root"]
    source_names = [row["path"] for row in verified.document["source"]["inventory"]]
    if reader.inventory(context) != source_names:
        raise ValueError("complete source inventory changed after admission")
    validated = []
    for row in verified.inventory:
        observation, _ = reader.captured[row["path"]]
        if (
            type(observation.data) is not bytes
            or len(observation.data) != row["bytes"]
            or sha256(observation.data) != row["sha256"]
            or observation.kind != row["kind"]
            or type(observation.mode) is not int
            or observation.mode != row["mode"]
            or observation.link_target != row.get("link_target")
        ):
            raise ValueError("captured input differs from actual admission")
        validated.append((row, observation))
    destination = Path(destination)
    if (
        destination.exists()
        or destination.is_symlink()
        or destination.resolve().is_relative_to(reader.root)
    ):
        raise ValueError("fresh independent input clone required")
    destination.mkdir(mode=0o700)
    epoch = verified.document["determinism"]["epoch"]
    for row, observation in validated:
        path = destination / row["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        if observation.kind == "symlink":
            path.symlink_to(observation.link_target)
        else:
            with path.open("xb") as stream:
                stream.write(observation.data)
            path.chmod(observation.mode)
            os.utime(path, (epoch, epoch), follow_symlinks=False)
    reader.assert_unchanged()
    if reader.inventory(context) != source_names:
        raise ValueError("complete source inventory changed during materialization")
    return destination


def prepare(raw, input_reader, accepted_policy, destination):
    """Admission precedes any clone creation; no builder is invoked here."""
    verified = admit(raw, input_reader, accepted_policy)
    return verified, materialize(verified, input_reader, destination)
