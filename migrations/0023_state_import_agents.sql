-- R151: widen only the reviewed family set. Earlier migration bytes stay frozen.
ALTER TABLE cortex_core.import_rows
    DROP CONSTRAINT import_rows_family_check,
    ADD CONSTRAINT import_rows_family_check CHECK (family IN ('projects', 'agents'));
