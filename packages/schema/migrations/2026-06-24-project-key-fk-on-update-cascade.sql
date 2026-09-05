-- 2026-06-24 project-key FK ON UPDATE CASCADE
--
-- migrate_project_key() renames cortex_projects.project_key in place. The two
-- FKs that reference cortex_projects(project_key) were ON DELETE CASCADE only,
-- so the parent-key UPDATE violated cortex_project_paths_project_key_fkey
-- ("key (project_key)=(...) is still referenced"). Adding ON UPDATE CASCADE
-- makes a supported project-key rename propagate to referencing rows instead
-- of erroring. Idempotent: drop-if-exists then re-add with the cascade.

ALTER TABLE public.cortex_project_paths
    DROP CONSTRAINT IF EXISTS cortex_project_paths_project_key_fkey;
ALTER TABLE public.cortex_project_paths
    ADD CONSTRAINT cortex_project_paths_project_key_fkey
    FOREIGN KEY (project_key) REFERENCES public.cortex_projects(project_key)
    ON UPDATE CASCADE ON DELETE CASCADE;

ALTER TABLE public.cortex_projects
    DROP CONSTRAINT IF EXISTS cortex_projects_parent_project_key_fkey;
ALTER TABLE public.cortex_projects
    ADD CONSTRAINT cortex_projects_parent_project_key_fkey
    FOREIGN KEY (parent_project_key) REFERENCES public.cortex_projects(project_key)
    ON UPDATE CASCADE;
