"""S5 install record module. RED skeleton: it exists so the tests fail on behaviour, never on import; it writes nothing and reads nothing."""


class RecordRefusal(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def record_object(prefix, release_id, kind, name, ident, *, owner_uid=None):
    return None


def forget_object(prefix, name, *, owner_uid=None):
    return False


def read_record(prefix, *, owner_uid=None):
    return None
