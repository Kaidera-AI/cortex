# REBUILD-NONRECON-001 — bounded PLAN ONLY for acceptance

OwnerBob,H-D477NEXTCortexcut; neverv0.1.003/live5500/8501. Separate frompreflightPR52. No implementation ornewdatabase execution bythisplan.

## Source facts / census gate

At frozen24ee4bf (baseabbb45af), Claudeingester tags messages.metadata.source=local-file and supplies providerclaude/source_kindclaude-session. API sessioningest upserts session_sources(provider,source_kind,source_path) then replaces messages only for that exactsession/project. Manualsavechat tags sessionnotes.source and messagemetadata.source=manual-save-chat and creates no guaranteedlocaltranscript. Rebuild itself currently adds NO distinct rebuild-owned tag: it invokes ordinaryingesters, which produce sharedingestmetadata. A source_kind alone does not prove currentlocalreconstructibility.

Firstimplementationgate: read-only census on NEW labelledthrowawayDB built from actualowned migrations/schema (no productiondump). Populate only syntheticmanual/transcript/untaggedlegacy/crossproject/session_sources rows. SELECT/GROUPBY sessions.notes.source, messages.metadata.source/provider, session_sources.provider/source_kind and joincoverage; record everyknownsource/null combination, FK/constraint/cascade behavior and codewriter mapping. No secrets/content in output. Requirememoryfree>=35%, max1GiB/2CPU,networknone/no publishedports, cleanupfinally. Missingextensions/schema admission meansSTOP/report, not substitute guessedDDL. This census is PLANNED, NOT_RUN here; no claim that staticvalues are exhaustiveactualDBvalues.

## Selection policy / files

Update packages/cli/cortex-rebuild-history and its own small test; if required, add a bounded provenance predicate helper (no broadAPI/schema change).

Prefer POSITIVE selection by a dedicated reliable rebuild-owned tag if census provesone exists. Currentstaticcheck findsnone, so use exactidentified inputsessionUUIDs intersected with project+session_sources matching known transcriptprovider/kind AND validated available sourcepath. Preserve known nontranscriptsources (manual-save-chat), unknownsources and all untaggedambiguous legacy rows. Never use an unrestricted projectDELETE or negative-list alone that silently treats unknownsources asreconstructible.

Untaggedlegacyduplicate policy: existing sessioningest already upserts exactsessionUUID and replaces same-session messages atomically. For discoveredUUIDs without trustworthy provenance, reconcile only that existingidentity via the ordinaryingester, not bypurgingalluntaggedrows. Ambiguous/malformedUUID/provenance conflicts refused/reported forownerdecision; no guessedcontentdeduplication/newsessionminting. Session/source-FKbehavior mustpasscensus before selecting deleteorder. Preserve crossprojectrows always.

## RED / proof / mutant

CommitactualthrowawayDBRED: syntheticmanual-save-chat session+message survives rebuild alongside validtranscript; transcript reingest does notduplicate; untaggedexistingidentity reconciles withoutduplicating; unrelateduntagged/manual/crossprojectrows remain. ExistingprojectwideDELETE mustfailmanualsurvivalAssertionError. Include stale/missing/unreadableinput and sourceprovenancemismatch refusal beforemutation. GREENagainst unchangedoracles; mutantrestoringunscopedmessages/sessionDELETE mustkillmanualsurvival test. Freshsource/originSHA+schema+resource/cleanupreceipts, separatePR, Veraexactreview beforemerge.

## Risks / acceptance needed

Planchoosesconservative preservation ratherthan sweepingnegativeexclusion: source tagsare sharedingesttags, not rebuildownership. No completeatomicrebuild guarantee; partialparserfailures remain separate. Kaiaccept/rework exactselection and ambiguouslegacyrefusal policy beforecode. Fullruntime/native/release/security/livegatesremainheld. PreflightPR52reviewunchanged; Bob monitorsnewDO NOW afterplanhandback.

## Accepted policy amendment / implementation

Kai12:49/13:32/14:52: no project-wide purge. Re-ingest only discovered UUIDs through ordinary ingester, preserving manual/unknown/untagged identities. Orphan list covers missing/unreadable source paths; dry-run prints replacement/preservation/refusal UUIDs. Codex parser absent: preserve and explicitly report, no implicit data loss. Claude parser absence refuses execution before any ingest; unsupported Codex is a reported preservation and does not block eligible Claude. Cross-project UUID collision refused; unknown message source overrides otherwise matching transcript provenance. At fresh newmainb10c54e no rebase.

Census used authoritative packages/schema/cortex-schema-full.sql in existing upstream pgvectorPG18 isolated resource with real identity constraints. Schema/register synthetic project before insert (initial unregistered project refusal retained). Source inputs/mac mktemp helper isolated; actual Claude parser executed with fake local API transport to syntheticPG, not liveAPI. Existing preflight tests adjusted for new supported behavior: Codex report+preserve succeeds, empty-input history is preserved (strongerrowcountassertions). No existing preservation assertion removed.
