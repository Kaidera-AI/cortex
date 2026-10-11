"""Ordered guard application. Effects remain behind the checked-command adapter."""
from __future__ import annotations

import doctor
import payload
import restoration
from admission import validate_release
from custody import bound_json, require


class Workflow:
    def __init__(self, authority, store, *, signature_runner):
        self.authority = authority
        self.store = store
        self.signature_runner = signature_runner

    def preallocate(self, measured_rows):
        validate_release(self.authority, signature_runner=self.signature_runner)
        self.authority.check_measured_receipts(measured_rows)
        return self.store.append("PRE_ALLOC_CHECKED", {"checks": measured_rows,
            "admitted_command_inputs": self.authority.B["phase_command_inputs"]["PRE_ALLOC_CHECKED"]})

    def before_extract(self, actual_literals, delivery_row):
        # HOST_BOUND follows strict console binding/timers, not extraction.
        self.store.current("HOST_BOUND")
        validate_release(self.authority, signature_runner=self.signature_runner)
        return payload.validate_literals(self.authority, actual_literals, delivery_row)

    def extract(self, actual_literals, delivery_row, extractor):
        with self.store.locked():
            values = self.before_extract(actual_literals, delivery_row)
            return extractor(values)

    def before_image_load(self, actual_literals, delivery_row, projects_environment):
        self.store.current("HOST_BOUND")
        manifest = validate_release(self.authority, signature_runner=self.signature_runner)
        closed = payload.pre_load(self.authority, manifest, actual_literals, delivery_row)
        projects = payload.validate_projects(self.authority, actual_literals, delivery_row, projects_environment)
        return {**closed, **projects}

    def load_images(self, actual_literals, delivery_row, projects_environment, image_loader):
        with self.store.locked():
            receipt = self.before_image_load(actual_literals, delivery_row, projects_environment)
            return image_loader(receipt)

    def initial_install(self, install_row, observed, *, doctor_runner):
        with self.store.locked():
            host = self.store.current("HOST_BOUND")
            require(observed.get("instance_id") == host["instance_id"], "initial_install_host")
            initial = doctor.observe(self.authority, install_row, runner=doctor_runner)
            observed = {**observed, "kos_install_receipt": install_row, "initial_appdb_doctor": initial,
                        "admitted_command_inputs": self.authority.B["phase_command_inputs"]["INSTALLED"]}
            return self.store.append_locked("INSTALLED", observed)

    def check_doctor(self, *, runner):
        rows, _ = self.store.read()
        require(rows[-1]["phase"] in {"INSTALLED", "ARMED", "REBOOT_VERIFIED"}, "workflow_current_phase")
        installed = self.store.phase("INSTALLED")
        return doctor.observe(self.authority, installed["kos_install_receipt"], runner=runner,
                              initial=installed["initial_appdb_doctor"])

    def arm(self, armed_row):
        self.store.current("INSTALLED")
        installed, host = self.store.phase("INSTALLED"), self.store.phase("HOST_BOUND")
        receipt = restoration.armed(self.authority, installed, host, armed_row)
        return self.store.append("ARMED", {"instance_id": host["instance_id"], "armed_receipt": armed_row,
            "boot_id_before": receipt["boot_id_before"],
            "admitted_command_inputs": self.authority.B["phase_command_inputs"]["ARMED"]})

    def recovered(self, recovery_row):
        self.store.current("ARMED")
        installed = self.store.phase("INSTALLED")
        armed_row = self.store.phase("ARMED")["armed_receipt"]
        initial_armed = bound_json(armed_row)
        receipt = restoration.recovered(self.authority, installed, initial_armed, recovery_row)
        return self.store.append("REBOOT_VERIFIED", {"instance_id": receipt["instance_id"],
            "boot_id_after": receipt["boot_id_after"], "recovery_receipt": recovery_row,
            "admitted_command_inputs": self.authority.B["phase_command_inputs"]["REBOOT_VERIFIED"]})
