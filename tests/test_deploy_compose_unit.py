"""Deployment bind mounts must work on SELinux-enforcing hosts."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _repo_path(source: str) -> bool:
    path = Path(source)
    if path.is_absolute():
        return path.is_relative_to(ROOT)
    return source in {".", ".."} or "/" in source or source.startswith((".", "$"))


def test_repo_bind_mounts_have_selinux_relabel() -> None:
    manifests = sorted((ROOT / "deploy").glob("compose*.yaml"))
    assert manifests, "no deployment Compose manifests found"
    missing = []
    for manifest in manifests:
        services = yaml.safe_load(manifest.read_text())["services"]
        for name, service in services.items():
            for mount in service.get("volumes", ()):
                if isinstance(mount, str):
                    source, separator, remainder = mount.partition(":")
                    if not separator or not _repo_path(source):
                        continue
                    _, separator, options = remainder.rpartition(":")
                    relabel = separator and {"z", "Z"} & set(options.split(","))
                else:
                    if mount.get("type") != "bind":
                        continue
                    source = str(mount["source"])
                    if Path(source).is_absolute() and not _repo_path(source):
                        continue
                    relabel = (mount.get("bind") or {}).get("selinux") in {"z", "Z"}
                if not relabel:
                    missing.append(f"{manifest.name} {name}: {source} lacks z/Z relabel")
    assert not missing, "\n".join(missing)
