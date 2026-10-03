-- Every active project gets one canonical console service identity. The console
-- selects a project-bound token for ordinary RLS reads and an instance-admin
-- scope only for reviewed operator routes.

INSERT INTO public.roles (
    project, name, default_capabilities, description, is_builtin, source_file,
    created_at, updated_at
)
SELECT
    project.project_key,
    'service',
    '{"designation":"service","writer_scope":"work"}'::jsonb,
    'Local Cortex service principal',
    true,
    'service-auth-bootstrap',
    now(),
    now()
FROM public.cortex_projects AS project
WHERE project.status = 'active'
ON CONFLICT (project, name) DO UPDATE SET
    default_capabilities = public.roles.default_capabilities || EXCLUDED.default_capabilities,
    updated_at = now();

INSERT INTO public.agents (
    name, project, role, capabilities, status, runtime_state
)
SELECT
    'console',
    project.project_key,
    'service',
    '{"designation":"service","writer_scope":"work","keep_visible":true,"visibility":"active"}'::jsonb,
    'available',
    jsonb_build_object(
        'agent', 'console',
        'project', project.project_key,
        'registered_by', 'service-auth-bootstrap'
    )
FROM public.cortex_projects AS project
WHERE project.status = 'active'
ON CONFLICT (name, project) DO UPDATE SET
    role = EXCLUDED.role,
    status = EXCLUDED.status,
    capabilities = COALESCE(public.agents.capabilities, '{}'::jsonb) || EXCLUDED.capabilities,
    runtime_state = COALESCE(public.agents.runtime_state, '{}'::jsonb) || EXCLUDED.runtime_state;
