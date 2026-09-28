"""Cortex v2 ingestion connectors (R11).

Session/message/local-state/diary/save-chat connectors with atomic
replacement and parse-loss protection. All canonical content writes go
through the frozen W1 content use cases (``content.ingest_content`` and
``content.change_status``); this module never writes content tables itself.
The connector ledger lives in ``cortex_context`` (migration 0006).
"""
