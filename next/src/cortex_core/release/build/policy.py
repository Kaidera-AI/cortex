"""Explicit engine policy; native admission is not deployment or allocation."""

from pathlib import Path
import re


def build_argv(engine, source, output, epoch):
    if type(epoch) is not int or not 0 <= epoch <= 253402300799:
        raise ValueError("invalid deterministic epoch")
    if not Path(engine).is_absolute():
        raise ValueError("explicit engine executable required")
    return [
        str(engine),
        "build",
        "--source-date-epoch",
        str(epoch),
        "--rewrite-timestamp",
        "--build-arg",
        "SOURCE_DATE_EPOCH=" + str(epoch),
        "--no-cache",
        "--layers=false",
        "--file",
        str(Path(source) / "Dockerfile"),
        "--iidfile",
        str(output),
        str(source),
    ]


def verify_native(observed, platform):
    arches = {"linux/amd64": {"amd64", "x86_64"}, "linux/arm64": {"arm64", "aarch64"}}
    if (
        platform not in arches
        or observed.get("os") != "linux"
        or observed.get("arch") not in arches[platform]
        or type(observed.get("uid")) is not int
        or observed["uid"] <= 0
        or observed.get("rootless") is not True
        or observed.get("emulated", False) is not False
    ):
        raise ValueError("native nonroot rootless architecture admission refused")
    return True


def cleanup_paths(creator_uid, paths):
    if type(creator_uid) is not int or creator_uid < 0:
        raise ValueError("invalid creating uid")
    for row in paths:
        path = Path(row["path"])
        if path.is_absolute() or ".." in path.parts or row.get("uid") != creator_uid:
            raise ValueError("cleanup outside creating owner or scope")
    return paths


def public_environment(values, allowed):
    forbidden = re.compile(
        r"(TOKEN|SECRET|PASSWORD|PRIVATE|DSN|CREDENTIAL|API_KEY)", re.I
    )
    if set(values) - set(allowed) or any(forbidden.search(k) for k in values):
        raise ValueError("unadmitted or private environment")
    return dict(values)


def runtime_argv(
    engine, image, uid, home, project_source, project_destination, arguments
):
    """Construct the smoke command without replacing the image's actual entrypoint.

    Inputs must come from the admitted module runtime contract. This is command
    construction only; executing it remains part of the separately gated native run.
    """
    if not Path(engine).is_absolute() or type(uid) is not int or uid < 1:
        raise ValueError("explicit engine and final nonroot uid required")
    if (
        not isinstance(image, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", image) is None
    ):
        raise ValueError("immutable local image identity required")
    for value in (home, project_source, project_destination):
        if (
            not isinstance(value, str)
            or not Path(value).is_absolute()
            or ".." in Path(value).parts
            or any(c in value for c in (",", "\x00", "\n", "\r"))
        ):
            raise ValueError("unambiguous absolute runtime path required")
    if (
        not isinstance(arguments, (list, tuple))
        or not arguments
        or any(not isinstance(a, str) or "\x00" in a for a in arguments)
    ):
        raise ValueError("actual module command arguments required")
    return [
        str(engine),
        "run",
        "--pull=never",
        "--rm",
        "--read-only",
        "--network=none",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--user",
        str(uid),
        "--env",
        "HOME=" + home,
        "--mount",
        "type=bind,source="
        + project_source
        + ",destination="
        + project_destination
        + ",ro",
        image,
        *arguments,
    ]
