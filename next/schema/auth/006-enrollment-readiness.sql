-- CX-GAP-2b: public enum status through a private receipt-state read path.
CREATE TYPE auth.enrollment_readiness_state AS ENUM
    ('enrollment_required','ready','unavailable');
REVOKE ALL ON TYPE auth.enrollment_readiness_state FROM PUBLIC;
GRANT USAGE ON TYPE auth.enrollment_readiness_state TO "kaidera-runtime-core-request";

CREATE FUNCTION auth.enrollment_readiness(p_installation uuid)
RETURNS auth.enrollment_readiness_state
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path=pg_catalog,auth,core,pg_temp AS $$
DECLARE default_count bigint;
        default_tenant uuid;
        default_project uuid;
        receipt record;
BEGIN
    IF p_installation IS NULL OR NOT EXISTS (
        SELECT 1 FROM core.installations WHERE id=p_installation
    ) THEN RETURN 'unavailable'::auth.enrollment_readiness_state; END IF;

    SELECT count(*) INTO default_count FROM core.tenants t
      JOIN core.projects p ON p.tenant_id=t.id
      WHERE t.installation_id=p_installation AND p.name='default';
    IF default_count IS DISTINCT FROM 1 THEN
        RETURN 'unavailable'::auth.enrollment_readiness_state;
    END IF;
    SELECT t.id,p.id INTO default_tenant,default_project FROM core.tenants t
      JOIN core.projects p ON p.tenant_id=t.id
      WHERE t.installation_id=p_installation AND p.name='default';

    SELECT r.tenant_id,r.project_id,r.principal_id INTO receipt
      FROM auth.first_identity_receipts r WHERE r.installation_id=p_installation;
    IF NOT FOUND THEN
        IF EXISTS (SELECT 1 FROM auth.principals p JOIN core.tenants t
            ON t.id=p.tenant_id WHERE t.installation_id=p_installation)
           OR EXISTS (SELECT 1 FROM core.record_aliases a JOIN core.tenants t
            ON t.id=a.tenant_id WHERE t.installation_id=p_installation
              AND a.source_namespace='cortex.core.identity')
           OR EXISTS (SELECT 1 FROM core.records r JOIN core.tenants t
            ON t.id=r.tenant_id WHERE t.installation_id=p_installation
              AND r.kind='core.identity') THEN
            RETURN 'unavailable'::auth.enrollment_readiness_state;
        END IF;
        RETURN 'enrollment_required'::auth.enrollment_readiness_state;
    END IF;

    IF receipt.tenant_id IS DISTINCT FROM default_tenant
       OR receipt.project_id IS DISTINCT FROM default_project
       OR NOT EXISTS (
           SELECT 1 FROM auth.principals p JOIN auth.project_grants g
             ON (g.tenant_id,g.principal_id)=(p.tenant_id,p.id)
           WHERE (p.tenant_id,p.id)=(receipt.tenant_id,receipt.principal_id)
             AND p.kind='human' AND p.identity_adopted IS TRUE
             AND g.project_id=default_project
             AND g.permissions @> ARRAY['read','write']::text[]
             AND cardinality(g.permissions)=2
             AND g.roles=ARRAY[]::text[]
       ) THEN RETURN 'unavailable'::auth.enrollment_readiness_state; END IF;
    RETURN 'ready'::auth.enrollment_readiness_state;
END;
$$;
ALTER FUNCTION auth.enrollment_readiness(uuid)
    OWNER TO "kaidera-runtime-core-verifier";
REVOKE ALL ON FUNCTION auth.enrollment_readiness(uuid)
    FROM PUBLIC,"kaidera-runtime-core-installer";
GRANT EXECUTE ON FUNCTION auth.enrollment_readiness(uuid)
    TO "kaidera-runtime-core-request";
