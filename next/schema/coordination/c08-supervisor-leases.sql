-- Installation supervisor state, distinct from tenant/project job leases.
CREATE SCHEMA IF NOT EXISTS coordination;
CREATE TABLE coordination.supervisor_leases (
    installation_id uuid PRIMARY KEY REFERENCES core.installations(id),
    holder uuid NOT NULL,
    fence bigint NOT NULL CHECK (fence > 0),
    expires_at timestamptz NOT NULL
);
CREATE FUNCTION coordination.supervisor_fence_no_regression() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.fence < OLD.fence THEN
        RAISE EXCEPTION 'supervisor fence regression' USING ERRCODE='55000';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER supervisor_fence_monotonic BEFORE UPDATE ON coordination.supervisor_leases
    FOR EACH ROW EXECUTE FUNCTION coordination.supervisor_fence_no_regression();
ALTER TABLE coordination.supervisor_leases ENABLE ROW LEVEL SECURITY;
ALTER TABLE coordination.supervisor_leases FORCE ROW LEVEL SECURITY;
CREATE POLICY supervisor_installation_scope ON coordination.supervisor_leases
    USING (installation_id = nullif(current_setting('cortex.installation_id',true),'')::uuid)
    WITH CHECK (installation_id = nullif(current_setting('cortex.installation_id',true),'')::uuid);
