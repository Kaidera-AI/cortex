# UPSTREAM (ship/install roles)
Nothing copied. Uses PostgreSQL (PostgreSQL licence) `psql`, `initdb`, `pg_ctl` 18.x as an executor and in tests; the role flags come from this repo's runtime-operations-v1.json; the group-creating DO block used by the tests is read from `next/schema/auth/002-isolation.sql` at run time.
