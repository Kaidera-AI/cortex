"""ship/install roles: interface stub (RED commit). Implementation follows in the next commit."""


class RoleRefusal(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def load_roles(path=None):
    raise RoleRefusal('SURFACE_ABSENCE:load_roles')


def physical_name(logical):
    raise RoleRefusal('SURFACE_ABSENCE:physical_name')


def render_pre(roles, secret_dir):
    raise RoleRefusal('SURFACE_ABSENCE:render_pre')


def render_post(roles):
    raise RoleRefusal('SURFACE_ABSENCE:render_post')


def render_close(roles, seed_ack):
    raise RoleRefusal('SURFACE_ABSENCE:render_close')
