-- memory-efficiency E1: set a sane ivfflat.probes default (was 1 → poor recall once
-- lists>1). Applies to NEW connections; cortex-api is bounced after this migration so its
-- pool picks it up. Single statement, txn-safe.
ALTER DATABASE platform_agent_memory SET ivfflat.probes = 10;
