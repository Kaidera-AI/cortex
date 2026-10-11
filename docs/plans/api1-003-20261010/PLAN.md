# API1-003 LISTEN disconnect — next v0.1.004 source fix

Owner Bob, Kai item8. Separate branch/PR from freshmain, no rebase/live/DB operation. Only listen_for_team_events in main.py, own tests and plan.

Replace forever-pending Future with a connection termination Event. Register installed asyncpg add_termination_listener before subscription; callback resets readiness for its owned connection and signals Event. On termination take the existing retry path. Remove termination callback during finally before closing the owned connection; cancellation must clear readiness and ownership. Preserve existing retry delay and notification callbacks.

RED against Mike's frozen test_ready_listener_disconnect_reconnects assertion oracle. Necessary protocol amendment: its Connection double only has add_listener/remove_listener/close and a closed field, not asyncpg's termination-listener methods. Extend ONLY that double with actual synchronous add/remove_termination_listener and drop callback invocation; preserve every frozen assertion and all time controls. Never count unexpected AttributeError as GREEN. Bind actual source AST; no fullAPI import/host contact. Add deterministic double controls for reconnect, shutdown cleanup and stale old termination callback. Commit RED before source; GREEN then forever-Future mutant must fail same readiness assertion. >=35% fresh memory gate. Independent exact-SHA Vera review before merge.

Accept the protocol-double amendment explicitly before implementation. No native PostgreSQL or installed runtime qualification claimed; frozen assertion and actual source-body receipts retained. Installed asyncpg API was inspected locally; no guessed interface.

Kai15:08 amendment accepted. Add inspect.signature equality control for both double termination methods. Forward merge new main df4f5a63 after PR62; never rebase.
