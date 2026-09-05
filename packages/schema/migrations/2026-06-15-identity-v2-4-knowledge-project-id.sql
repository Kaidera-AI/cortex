-- Identity v2 clean cutover: knowledge rows must carry project_id for every
-- registered project-scoped row. Underscore-prefixed system scopes such as
-- `_global` and `_local_state` remain nullable by design.

BEGIN;

UPDATE knowledge k
   SET project_id = p.id
  FROM cortex_projects p
 WHERE k.project = p.project_key
   AND k.project <> '_global'
   AND k.project_id IS NULL;

CREATE INDEX IF NOT EXISTS idx_knowledge_project_id
    ON knowledge (project_id)
    WHERE project_id IS NOT NULL;

ALTER TABLE knowledge
    DROP CONSTRAINT IF EXISTS ck_identity_v2_knowledge_project_id_present;

ALTER TABLE knowledge
    ADD CONSTRAINT ck_identity_v2_knowledge_project_id_present
    CHECK (project LIKE '\_%' ESCAPE '\' OR project_id IS NOT NULL);

COMMIT;
