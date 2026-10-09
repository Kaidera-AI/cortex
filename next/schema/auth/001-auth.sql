CREATE SCHEMA auth;
CREATE TABLE auth.principals (
    tenant_id uuid NOT NULL REFERENCES core.tenants,
    id uuid NOT NULL,
    name text NOT NULL CHECK (length(name) BETWEEN 1 AND 256),
    disabled boolean NOT NULL DEFAULT false,
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, name)
);
CREATE TABLE auth.credentials (
    tenant_id uuid NOT NULL,
    id uuid NOT NULL,
    principal_id uuid NOT NULL,
    key_digest text NOT NULL UNIQUE CHECK (key_digest ~ '^[0-9a-f]{64}$'),
    expires_at timestamptz,
    revoked_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, principal_id) REFERENCES auth.principals (tenant_id, id)
);
CREATE TABLE auth.project_grants (
    tenant_id uuid NOT NULL,
    project_id uuid NOT NULL,
    principal_id uuid NOT NULL,
    permissions text[] NOT NULL CHECK (cardinality(permissions) > 0 AND permissions <@ ARRAY['read','write','control','owner','admin']),
    PRIMARY KEY (tenant_id, project_id, principal_id),
    FOREIGN KEY (tenant_id, project_id) REFERENCES core.projects (tenant_id, id),
    FOREIGN KEY (tenant_id, principal_id) REFERENCES auth.principals (tenant_id, id)
);
CREATE TABLE auth.permission_generations (
    tenant_id uuid NOT NULL,
    project_id uuid NOT NULL,
    generation bigint NOT NULL DEFAULT 1 CHECK (generation > 0),
    PRIMARY KEY (tenant_id, project_id),
    FOREIGN KEY (tenant_id, project_id) REFERENCES core.projects (tenant_id, id)
);
-- No runtime roles or grants here. C04 installs forced RLS and private auth ports.
