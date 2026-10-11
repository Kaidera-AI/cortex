"""Use the signed BF doctor at its receipt-selected private managed state."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess

from custody import bound_file, bound_json, parse_json, physical, require


IDENTITY_FIELDS = ("runtime_identity_sha256", "port", "database", "user", "platform",
                   "container_id", "volume", "network", "image", "instance")


def command(authority, install_row):
    R = bound_json(install_row)
    K = authority.B.get("kos_signed_binding")
    require(isinstance(K, dict), "kos_signed_slots_unbound")
    require(R.get("mode") == "fresh" and R.get("outcome") == "installed", "kos_install_not_complete")
    for receipt_key, expected_key in (("version", "version"), ("platform", "platform"),
                                     ("release_sha256", "manifest_sha256"), ("root", "app_root"),
                                     ("data_root", "data_root"), ("runtime", "runtime"), ("service", "service")):
        require(R.get(receipt_key) == K.get(expected_key), "kos_install_binding")
    require(R["platform"] == "linux-x86_64", "kos_native_platform")
    app = R.get("appdb", {})
    require(app.get("managed") is True and app.get("port_chosen_by_the_manager") is True
            and type(app.get("port")) is int and 1024 <= app["port"] <= 65535, "appdb_selected_port")
    runtime = physical(Path(R["root"]) / "runtime" / R["runtime"])
    alias = runtime / "python/bin/python3"
    require(alias.is_symlink() and os.readlink(alias) == K.get("python_alias_target") == "python3.12",
            "signed_python_alias")
    target = alias.resolve(strict=True)
    require(target == runtime / "python/bin/python3.12", "external_python_alias")
    python = bound_file({"path": str(target), "bytes": K["python"]["bytes"], "sha256": K["python"]["sha256"]})
    script = runtime / "scripts/install/appdb-managed.py"
    bound_file({"path": str(script), "bytes": K["doctor"]["bytes"], "sha256": K["doctor"]["sha256"]})
    module = runtime / "local-cortex/console/app/managed_appdb.py"
    bound_file({"path": str(module), "bytes": K["managed_appdb"]["bytes"], "sha256": K["managed_appdb"]["sha256"]})
    config = physical(Path(R["data_root"]) / "secrets")
    require(config.is_dir() and config.stat().st_uid == os.getuid(), "kos_config_owner")
    require(config.stat().st_mode & 0o777 == 0o700, "kos_config_private_mode")
    return [str(python), str(script), "doctor"], config, app["port"]


def observe(authority, install_row, *, runner=subprocess.run, initial=None):
    argv, config, port = command(authority, install_row)
    env = {k: os.environ[k] for k in ("PATH", "HOME", "XDG_RUNTIME_DIR") if k in os.environ}
    env["KOS_CONFIG_DIR"] = str(config)
    result = runner(argv, env=env, capture_output=True, timeout=60)
    require(result.returncode == 0, "appdb_doctor_refused")
    D = parse_json(result.stdout)
    require(D.get("status") == "ready" and D.get("port") == port
            and D.get("platform") == "linux/amd64" and D.get("database") == "harness_app"
            and D.get("user") == "harness", "appdb_doctor_effect")
    require(all(key in D for key in IDENTITY_FIELDS), "doctor_identity_incomplete")
    if initial is not None:
        require(all(type(D[key]) is type(initial.get(key)) and D[key] == initial[key]
                    for key in IDENTITY_FIELDS), "doctor_identity_drift")
    return {"status": D["status"], **{key: D[key] for key in IDENTITY_FIELDS}}
