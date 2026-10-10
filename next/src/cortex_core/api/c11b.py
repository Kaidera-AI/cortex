"""C11b concrete C05 record port; RED interface before implementation."""


class C11bRecordPort:
    def __init__(self, connection_factory, installation_id, projects):
        self.connection_factory = connection_factory
        self.installation_id = installation_id
        self.projects = projects

    async def principal(self, scope):
        raise PermissionError('not implemented')

    async def write_memory(self, principal, scope, request_key):
        raise PermissionError('not implemented')

    async def read_record(self, principal, scope, record_id):
        raise PermissionError('not implemented')
