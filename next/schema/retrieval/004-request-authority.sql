-- Forward-only C04 authority for retrieval tables created after auth-0002.
-- Existing projection policies are permissive GUC scopes for module roles.
-- The request role additionally requires the private C04 transaction binding.
DO $$
DECLARE relation_name text; write_check text;
BEGIN
    FOREACH relation_name IN ARRAY ARRAY[
        'search_state', 'search_sources', 'search_vectors',
        'query_embeddings', 'graph_generations', 'graph_state',
        'graph_applied', 'graph_nodes', 'graph_edges'
    ] LOOP
        EXECUTE format('GRANT SELECT ON TABLE retrieval.%I TO "kaidera-runtime-core-request"',
                       relation_name);
        write_check := CASE WHEN relation_name='query_embeddings'
            THEN 'auth.has_access(tenant_id,project_id,''read'')'
            ELSE 'false' END;
        EXECUTE format('CREATE POLICY c11b3_request_bound ON retrieval.%I '
            'AS RESTRICTIVE FOR ALL TO "kaidera-runtime-core-request" '
            'USING (auth.has_access(tenant_id,project_id,''read'')) '
            'WITH CHECK (%s)', relation_name, write_check);
    END LOOP;
END;
$$;

-- Only the disposable query cache is mutable through a credentialed read.
-- Search's SELECT FOR SHARE needs UPDATE privilege; its restrictive CHECK
-- remains false, so the request role cannot change projection state.
GRANT UPDATE ON TABLE retrieval.search_state
    TO "kaidera-runtime-core-request";
GRANT INSERT,UPDATE,DELETE ON TABLE retrieval.query_embeddings
    TO "kaidera-runtime-core-request";
GRANT USAGE ON SEQUENCE retrieval.query_embedding_fences
    TO "kaidera-runtime-core-request";
