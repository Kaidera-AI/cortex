-- Bind managed code-graph storage to one immutable project/root generation.
-- Existing graph directories used mutable project-name keys, so this migration
-- deliberately holds every existing project for one explicit full rebuild into
-- its new generation-bound directory. API root moves and deleted-project
-- reactivation apply the same hold thereafter.
ALTER TABLE cortex_projects
    ADD COLUMN IF NOT EXISTS graph_generation uuid DEFAULT gen_random_uuid();

ALTER TABLE cortex_projects
    ADD COLUMN IF NOT EXISTS graph_requires_full_rebuild boolean DEFAULT TRUE;

UPDATE cortex_projects
   SET graph_generation = gen_random_uuid()
 WHERE graph_generation IS NULL;

UPDATE cortex_projects
   SET graph_requires_full_rebuild = TRUE
 WHERE graph_requires_full_rebuild IS NULL;

ALTER TABLE cortex_projects
    ALTER COLUMN graph_generation SET DEFAULT gen_random_uuid(),
    ALTER COLUMN graph_generation SET NOT NULL,
    ALTER COLUMN graph_requires_full_rebuild SET DEFAULT TRUE,
    ALTER COLUMN graph_requires_full_rebuild SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conrelid = 'cortex_projects'::regclass
           AND conname = 'cortex_projects_graph_generation_key'
    ) THEN
        ALTER TABLE cortex_projects
            ADD CONSTRAINT cortex_projects_graph_generation_key
            UNIQUE (graph_generation);
    END IF;
END
$$;
