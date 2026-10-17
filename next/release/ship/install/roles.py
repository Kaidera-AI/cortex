"""ship/install roles v2 (rework): interface stub for the RED commit. Implementation follows in the next commit."""


class RoleRefusal(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


A_PURPOSES = ('bootstrap', 'migrator', 'api_request', 'probe', 'installer_enrollment')


def load_roles(path=None):
    raise RoleRefusal('SURFACE_ABSENCE:load_roles')


def bind(configs, roles=None):
    raise RoleRefusal('SURFACE_ABSENCE:bind')


def render_pre(binding):
    raise RoleRefusal('SURFACE_ABSENCE:render_pre')


def render_post(binding):
    raise RoleRefusal('SURFACE_ABSENCE:render_post')


def render_close(binding, seed_ack):
    raise RoleRefusal('SURFACE_ABSENCE:render_close')
