"""R151 synthetic source classes; native fixture remains minimal and socket-only."""
from contextlib import asynccontextmanager
from pathlib import Path
import pytest
from fixtures.state_import_native import native_table, state_cluster
ROOT = Path(__file__).resolve().parents[2]
SOURCE_DDL = "CREATE SCHEMA cortex;\nCREATE TABLE public.agents (\n    id uuid DEFAULT gen_random_uuid() NOT NULL,\n    name text NOT NULL,\n    project text NOT NULL,\n    role text,\n    model text,\n    capabilities jsonb,\n    created_at timestamp with time zone DEFAULT now(),\n    status text DEFAULT 'available'::text NOT NULL,\n    runtime_state jsonb DEFAULT '{}'::jsonb NOT NULL,\n    project_id uuid,\n    actor_id uuid\n);\nALTER TABLE public.agents ADD PRIMARY KEY (id);\nCREATE TABLE public.agent_profiles (\n    id uuid DEFAULT gen_random_uuid() NOT NULL,\n    project text DEFAULT '_global'::text NOT NULL,\n    agent_name text NOT NULL,\n    profile_kind text NOT NULL,\n    role text,\n    source_file text NOT NULL,\n    profile_text text NOT NULL,\n    metadata jsonb,\n    updated_at timestamp with time zone DEFAULT now(),\n    project_id uuid,\n    actor_id uuid,\n    CONSTRAINT agent_profiles_profile_kind_check CHECK ((profile_kind = ANY (ARRAY['identity'::text, 'role'::text])))\n);\nALTER TABLE public.agent_profiles ADD PRIMARY KEY (id);\nCREATE TABLE public.roles (\n    project text NOT NULL,\n    name text NOT NULL,\n    default_capabilities jsonb DEFAULT '{}'::jsonb NOT NULL,\n    description text,\n    is_builtin boolean DEFAULT false NOT NULL,\n    source_file text,\n    created_at timestamp with time zone DEFAULT now() NOT NULL,\n    updated_at timestamp with time zone DEFAULT now() NOT NULL\n);\nALTER TABLE public.roles ADD PRIMARY KEY (project,name);\nCREATE TABLE cortex.agent_profiles (\n    id uuid DEFAULT gen_random_uuid() NOT NULL,\n    name character varying(120) NOT NULL,\n    role character varying(120) NOT NULL,\n    lane character varying(120),\n    reports_to character varying(120),\n    capabilities jsonb DEFAULT '{}'::jsonb NOT NULL,\n    status character varying(32) DEFAULT 'active'::character varying NOT NULL,\n    created_at timestamp with time zone DEFAULT now() NOT NULL,\n    updated_at timestamp with time zone DEFAULT now() NOT NULL,\n    customer_id uuid,\n    project_id uuid\n);\nALTER TABLE cortex.agent_profiles ADD PRIMARY KEY (id);\nCREATE TABLE cortex.roles (\n    id uuid DEFAULT gen_random_uuid() NOT NULL,\n    customer_id uuid NOT NULL,\n    project_id uuid NOT NULL,\n    name character varying(120) NOT NULL,\n    display_name character varying(160) NOT NULL,\n    description text,\n    permissions jsonb DEFAULT '{}'::jsonb NOT NULL,\n    is_builtin boolean DEFAULT false NOT NULL,\n    status character varying(16) DEFAULT 'active'::character varying NOT NULL,\n    created_at timestamp with time zone DEFAULT now() NOT NULL,\n    updated_at timestamp with time zone DEFAULT now() NOT NULL,\n    CONSTRAINT ck_roles_name_slug CHECK (((name)::text ~ '^[a-z][a-z0-9_-]{1,119}$'::text)),\n    CONSTRAINT ck_roles_status CHECK (((status)::text = ANY ((ARRAY['active'::character varying, 'deleted'::character varying])::text[])))\n);\nALTER TABLE cortex.roles ADD PRIMARY KEY (id);\nCREATE TABLE cortex.role_audit_events (\n    id uuid DEFAULT gen_random_uuid() NOT NULL,\n    customer_id uuid NOT NULL,\n    project_id uuid NOT NULL,\n    role_name character varying(120) NOT NULL,\n    action character varying(16) NOT NULL,\n    actor_scope character varying(32) NOT NULL,\n    actor_agent character varying(120),\n    actor_user_id uuid,\n    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,\n    created_at timestamp with time zone DEFAULT now() NOT NULL,\n    updated_at timestamp with time zone DEFAULT now() NOT NULL,\n    CONSTRAINT ck_role_audit_events_action CHECK (((action)::text = ANY ((ARRAY['create'::character varying, 'update'::character varying, 'delete'::character varying])::text[])))\n);\nALTER TABLE cortex.role_audit_events ADD PRIMARY KEY (id);\nGRANT USAGE ON SCHEMA cortex TO fixture_reader;\nGRANT SELECT ON public.agents,public.agent_profiles,public.roles,cortex.agent_profiles,cortex.roles,cortex.role_audit_events TO fixture_reader;"
AUTHORITY = (
    ("0001_core.sql", "cortex_auth.principals"),
    ("0001_core.sql", "cortex_auth.credentials"),
    ("0001_core.sql", "cortex_auth.scope_grants"),
    ("0002_w1_identity_memory.sql", "cortex_auth.actors"),
    ("0002_w1_identity_memory.sql", "cortex_auth.actor_bindings"),
)
@pytest.fixture(scope="session")
def agent_cluster(state_cluster):
    @asynccontextmanager
    async def pair():
        async with state_cluster() as value:
            await value["writer"].execute(SOURCE_DDL)
            for migration,name in AUTHORITY:
                await value["target"].execute(native_table(migration,name))
                await value["target"].execute(f"ALTER TABLE {name} ENABLE ROW LEVEL SECURITY; ALTER TABLE {name} FORCE ROW LEVEL SECURITY; CREATE POLICY fixture_migrator ON {name} FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true)")
            migration = ROOT / "migrations/0023_state_import_agents.sql"
            if migration.exists():
                await value["target"].execute(migration.read_text())
            yield value
    return pair
