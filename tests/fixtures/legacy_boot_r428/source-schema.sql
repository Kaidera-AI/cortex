CREATE TABLE public.agent_profiles (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    project text DEFAULT '_global'::text NOT NULL,
    agent_name text NOT NULL,
    profile_kind text NOT NULL,
    role text,
    source_file text NOT NULL,
    profile_text text NOT NULL,
    metadata jsonb,
    updated_at timestamp with time zone DEFAULT now(),
    project_id uuid,
    actor_id uuid,
    CONSTRAINT agent_profiles_profile_kind_check CHECK ((profile_kind = ANY (ARRAY['identity'::text, 'role'::text])))
);
ALTER TABLE public.agent_profiles ADD PRIMARY KEY(id);
GRANT SELECT ON public.agent_profiles TO fixture_reader;
CREATE TABLE public.rules (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    project text NOT NULL,
    rule_slug text NOT NULL,
    title text NOT NULL,
    body text NOT NULL,
    source_file text,
    version text DEFAULT '1'::text NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb,
    created_at timestamp with time zone DEFAULT now(),
    CONSTRAINT rules_status_check CHECK ((status = ANY (ARRAY['active'::text, 'deprecated'::text, 'draft'::text])))
);
ALTER TABLE public.rules ADD PRIMARY KEY(id);
GRANT SELECT ON public.rules TO fixture_reader;
CREATE TABLE public.agent_skills (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    project text NOT NULL,
    skill_slug text NOT NULL,
    name text,
    description text,
    skill_type text DEFAULT 'capability'::text NOT NULL,
    scope text DEFAULT 'project'::text NOT NULL,
    permission text,
    body_ref text,
    body_hash text,
    version text DEFAULT '1'::text NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    trust_tier text DEFAULT 'standard'::text NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb,
    created_at timestamp with time zone DEFAULT now(),
    CONSTRAINT agent_skills_scope_check CHECK ((scope = ANY (ARRAY['global'::text, 'project'::text, 'agent'::text]))),
    CONSTRAINT agent_skills_status_check CHECK ((status = ANY (ARRAY['active'::text, 'deprecated'::text, 'draft'::text])))
);
ALTER TABLE public.agent_skills ADD PRIMARY KEY(id);
GRANT SELECT ON public.agent_skills TO fixture_reader;
