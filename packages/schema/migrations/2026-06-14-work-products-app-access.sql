BEGIN;

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE work_products TO cortex_app;

ALTER TABLE work_products ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS work_products_project_isolation ON work_products;
CREATE POLICY work_products_project_isolation ON work_products
  USING (
      project = current_setting('cortex.project', TRUE)
      OR project = '_global'
  )
  WITH CHECK (
      project = current_setting('cortex.project', TRUE)
      OR project = '_global'
  );

COMMIT;
