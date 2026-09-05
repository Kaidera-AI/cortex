-- Remove stale concrete project defaults from project-scoped tables.
--
-- Missing project context must fail loudly or be supplied by the first-project /
-- add-project flow. It must never silently write rows to old development
-- projects such as tam or asw-connect.
ALTER TABLE public.agent_diaries ALTER COLUMN project DROP DEFAULT;
ALTER TABLE public.agent_sessions ALTER COLUMN project DROP DEFAULT;
ALTER TABLE public.agents ALTER COLUMN project DROP DEFAULT;
ALTER TABLE public.decisions ALTER COLUMN project DROP DEFAULT;
ALTER TABLE public.handoffs ALTER COLUMN project DROP DEFAULT;
ALTER TABLE public.lessons ALTER COLUMN project DROP DEFAULT;
ALTER TABLE public.messages ALTER COLUMN project DROP DEFAULT;
ALTER TABLE public.session_sources ALTER COLUMN project DROP DEFAULT;
ALTER TABLE public.sprints ALTER COLUMN project DROP DEFAULT;
ALTER TABLE public.tasks ALTER COLUMN project DROP DEFAULT;
