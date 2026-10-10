"""Private scoped, at-least-once delivery of committed canonical mutations."""
from contextlib import contextmanager
from dataclasses import dataclass

import psycopg

from .auth import AuthError, authorized


class OutboxError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class PublishedEvent:
    cursor: int
    envelope: dict
    payload: bytes


@dataclass(frozen=True)
class FeedPage:
    events: tuple
    head: int
    floor: int


def _publish(connection, limit):
    return tuple(PublishedEvent(*row) for row in connection.execute(
        'SELECT * FROM coordination.publish_outbox(%s)', (limit,)).fetchall())


def _limit(value):
    if type(value) is not int or not 1 <= value <= 1000:
        raise OutboxError('invalid_input')


class Outbox:
    def __init__(self, connection, credential, installation_id, project_id):
        self.connection = connection
        self.credential = credential
        self.installation_id = installation_id
        self.project_id = project_id

    @contextmanager
    def _auth(self, action):
        try:
            with authorized(self.connection, self.credential, self.installation_id, self.project_id, action) as scope:
                try:
                    yield scope
                except psycopg.errors.RaiseException as error:
                    code = {'outbox_expired': 'expired', 'outbox_invalid_input': 'invalid_input',
                            'outbox_forbidden': 'forbidden'}.get(error.diag.message_primary)
                    if code == 'forbidden':
                        raise AuthError(code) from None
                    raise OutboxError(code or 'core_unavailable') from None
        except AuthError as error:
            if error.code == 'core_unavailable':
                raise OutboxError(error.code) from None
            raise
        except psycopg.errors.RaiseException as error:
            code = {'outbox_expired': 'expired', 'outbox_invalid_input': 'invalid_input',
                    'outbox_forbidden': 'forbidden'}.get(error.diag.message_primary)
            if code == 'forbidden':
                raise AuthError(code) from None
            raise OutboxError(code or 'core_unavailable') from None
        except psycopg.Error:
            raise OutboxError('core_unavailable') from None

    def publish(self, limit=100):
        _limit(limit)
        with self._auth('control'):
            result = _publish(self.connection, limit)
        return result

    def page(self, after=0, limit=100):
        _limit(limit)
        if type(after) is not int or not 0 <= after < 2**63:
            raise OutboxError('invalid_input')
        with self._auth('read'):
            rows = self.connection.execute('SELECT * FROM coordination.outbox_page(%s,%s)', (after,limit)).fetchall()
            result = FeedPage(tuple(PublishedEvent(*row[:3]) for row in rows if row[0] is not None),rows[0][3],rows[0][4])
        return result

    def prune(self):
        with self._auth('control'):
            result = self.connection.execute('SELECT coordination.prune_outbox()').fetchone()[0]
        return result
