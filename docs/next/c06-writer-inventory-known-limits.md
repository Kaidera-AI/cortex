# C06 writer inventory — Known limits

The writer inventory is a source-bound review aid for the executable writer
forms covered by [`verify_writer_inventory.py`](../../next/scripts/verify_writer_inventory.py).
It is not a complete Python data-flow or reflection analysis. Its direct-call
checks for nonliteral `getattr`, `setattr`, `importlib.import_module`,
`__import__`, `eval`, and `exec` do not establish that an indirect spelling of
the same operation is safe.

The following forms are **outside the scanner's model and require human review**
when code that can reach a database writer changes:

- Aliased dynamic primitives: `getattr`, `setattr`, `import_module`,
  `__import__`, `eval`, or `exec` called under another name, including a
  computed attribute or module name.
- Reflection through `vars()`, `__dict__`, or `globals()` subscription.
- `operator.attrgetter` or `operator.methodcaller` with a computed target.
- Dynamic construction through `types` or `ctypes`.

Mike's **C06-PR51-002 round-4 follow-up probe** combined an aliased `getattr`
with a computed name and bypassed the direct-call check; Kai's 2026-10-11
00:17 ruling treats this as a known limit of the accepted threat model, not
a scanner rework. Reviewers must inspect these forms and their path to SQL
execution manually. The earlier committed direct-call and `_private` alias
controls remain described in [the C06 source proof](evidence/c06-source/reproduce.md)
and exercised by [the inventory tests](../../next/tests/outbox_inventory/test_outbox_inventory.py).
