"""Graph recipes use the parent's bound proof driver and shared phase admission."""
from pathlib import Path
import mutate_conductor as proof

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "src/cortex_core/modules/graph/pg_graph.py"
ROUTES = ROOT / "src/cortex_core/modules/graph/routes.py"
SQL = ROOT / "schema/retrieval/003-pg-graph.sql"


def mutation(path, name, before, after, test):
    return path, name, before, after, "test_pg_graph.GraphTests." + test


MUTATIONS = [
    mutation(SOURCE, "stale_revision_reads", "AND r.current_revision=a.source_revision AND NOT r.tombstone", "AND TRUE", "test_revision_and_tombstone_never_serve_stale_graph"),
    mutation(SOURCE, "wrong_extractor_admitted", 'if row["extractor_identity"] != self.identity:', 'if False and row["extractor_identity"] != self.identity:', "test_wrong_extractor_identity_is_unavailable"),
    mutation(SOURCE, "writer_grant_not_checked", 'self._tx(subject, "writer", project)', 'self._tx(subject, "read", project)', "test_revoked_writer_during_extraction_cannot_publish"),
    mutation(SOURCE, "generation_change_ignored", "if current_generation != generation:", "if False:", "test_changed_generation_completion_cannot_publish"),
    mutation(SOURCE, "stale_source_published",
        'if current is None or current["current_revision"] != source.revision or current["tombstone"] or current["kind"] != source.kind:\n                raise StaleGraph("stale_source")\n            existing',
        'if current is None or current["tombstone"] or current["kind"] != source.kind:\n                raise StaleGraph("stale_source")\n            existing', "test_stale_source_completion_cannot_publish"),
    mutation(SOURCE, "dry_run_extracts", "if not dry_run:\n            for item in sources:", "if True:\n            for item in sources:", "test_dry_run_does_not_extract_or_persist_facts"),
    mutation(SOURCE, "canonical_payload_mismatch_ignored", 'json.loads(bytes(canonical["body"])) != json.loads(json.dumps(asdict(facts)))', "False", "test_canonical_payload_must_match_projected_facts"),
    mutation(SOURCE, "pending_selection_starves", "AND ($5::boolean OR a.record_id IS NULL)", "AND ($5::boolean OR TRUE)", "test_pending_selection_advances_past_already_applied_records"),
    mutation(SOURCE, "unbounded_core_hydration", "async with asyncio.timeout(2):", "async with asyncio.timeout(30):", "test_whole_read_budget_bounds_stalled_core_hydration"),
    mutation(SOURCE, "unapproved_repo_accepted", 'if options["repo"] not in {scope.project_key, scope.repo}:', 'if False and options["repo"] not in {scope.project_key, scope.repo}:', "test_durable_build_intent_survives_adapter_restart_and_is_scoped"),
    mutation(SOURCE, "volatile_build_receipt",
        'jid = await self.enqueue_job(conn, scope, {**options, "repo": scope.repo, "generation": str(generation), "extractor_identity": self.identity})',
        "jid = uuid4()", "test_durable_build_intent_survives_adapter_restart_and_is_scoped"),
    mutation(SOURCE, "prune_preview_deletes",
        'if not dry_run:\n                await conn.execute("DELETE FROM retrieval.graph_generations',
        'if True:\n                await conn.execute("DELETE FROM retrieval.graph_generations', "test_scoped_prune_preserves_current_graph_other_projects_and_core"),
    mutation(SOURCE, "traversal_node_cap_removed", "if len(selected) == 1000:", "if len(selected) == 10000:", "test_large_neighborhood_and_provenance_are_clipped_explicitly"),
    mutation(SOURCE, "rebuild_requires_new_extraction_fact", 'existing_fact_id=packet["id"]', "existing_fact_id=None", "test_rebuild_uses_canonical_facts_without_extractor_or_new_fact"),
    mutation(ROUTES, "http_project_selector_ignored", 'project = request.headers.get("X-Project")', "project = None", "test_http_routes_seal_principal_and_validate_selectors"),
    mutation(SQL, "core_revision_fk_removed",
        "    FOREIGN KEY(tenant_id,project_id,record_id,source_revision)\n        REFERENCES core.record_revisions(tenant_id,project_id,record_id,revision),\n", "", "test_unknown_core_revision_refused_by_fk"),
    mutation(SQL, "rls_not_forced", "EXECUTE format('ALTER TABLE retrieval.%I FORCE ROW LEVEL SECURITY',t);",
        "EXECUTE format('ALTER TABLE retrieval.%I ENABLE ROW LEVEL SECURITY',t);", "test_tenant_project_rls_and_fresh_grants"),
]

if __name__ == "__main__":
    proof.MUTATIONS = MUTATIONS
    proof.run()
