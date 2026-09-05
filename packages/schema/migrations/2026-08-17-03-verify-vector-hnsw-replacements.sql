-- Never remove the legacy IVFFLAT indexes unless every full-precision vector
-- table already has a ready, valid HNSW replacement.
DO $$
DECLARE
    expected record;
BEGIN
    FOR expected IN
        SELECT * FROM (VALUES
            ('work_products', 'idx_work_products_embedding_hnsw'),
            ('lessons', 'idx_lessons_embedding_hnsw'),
            ('knowledge', 'idx_knowledge_embedding_hnsw'),
            ('decisions', 'idx_decisions_embedding_hnsw'),
            ('messages', 'idx_messages_embedding_hnsw')
        ) AS replacements(table_name, index_name)
    LOOP
        IF NOT EXISTS (
            SELECT 1
              FROM pg_catalog.pg_class AS idx
              JOIN pg_catalog.pg_namespace AS idx_ns
                ON idx_ns.oid = idx.relnamespace
              JOIN pg_catalog.pg_index AS meta ON meta.indexrelid = idx.oid
              JOIN pg_catalog.pg_class AS tbl ON tbl.oid = meta.indrelid
              JOIN pg_catalog.pg_namespace AS tbl_ns
                ON tbl_ns.oid = tbl.relnamespace
              JOIN pg_catalog.pg_am AS access_method ON access_method.oid = idx.relam
              JOIN pg_catalog.pg_attribute AS indexed_column
                ON indexed_column.attrelid = tbl.oid
               AND indexed_column.attnum = meta.indkey[0]
              JOIN pg_catalog.pg_opclass AS operator_class
                ON operator_class.oid = meta.indclass[0]
             WHERE idx_ns.nspname = 'public'
               AND tbl_ns.nspname = 'public'
               AND idx.relname = expected.index_name
               AND tbl.relname = expected.table_name
               AND access_method.amname = 'hnsw'
               AND meta.indisready
               AND meta.indisvalid
               AND meta.indpred IS NULL
               AND meta.indnatts = 1
               AND indexed_column.attname = 'embedding'
               AND operator_class.opcname = 'vector_cosine_ops'
        ) THEN
            RAISE EXCEPTION
                'refusing IVFFLAT removal: exact replacement %.% is unavailable',
                expected.table_name,
                expected.index_name;
        END IF;
    END LOOP;
END
$$;
