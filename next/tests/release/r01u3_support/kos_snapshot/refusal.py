"""A typed refusal: a stable code, a short detail and, when there is one, the install pointer (R1L-10).

The detail never carries a secret, a token or a digest of one."""

from __future__ import annotations


class Refusal(Exception):
    def __init__(self, code: str, detail: str = "", pointer: str = "", *, extra: dict | None = None) -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail
        self.pointer = pointer
        self.extra = extra  # structured, public facts about what was done and undone (an upgrade's rollback report)
