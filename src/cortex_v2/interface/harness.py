"""Deterministic harness mirror generation, drift detection and rollback
(R06).

A mirror is a pure function of pinned persona/rule/skill revisions: same
inputs → identical bytes and manifest hash. Skill *bodies* are deliberately
absent from the mirror (on-demand skills via ``skill.get``); the mirror
carries ``skills/INDEX.json`` references instead. Apply refuses to overwrite
a hand-edited mirror (typed ``harness_drift_detected``), and rollback plans
both restore changed files and remove files the rolled-back generation
introduced.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
import uuid
from dataclasses import dataclass
from typing import Any

from ..receipts import begin_command, commit_receipt, request_digest
from ..store import ApiProblem, ScopeContext
from .context import declare_policy_revision, select_persona_revision
from .models import (
    HarnessApplyRequest,
    HarnessDriftRequest,
    HarnessPreviewRequest,
    HarnessRollbackRequest,
)

HARNESS_GENERATOR_VERSION = "cortex.harness.v1"
MAX_MIRROR_FILES = 256
MANIFEST_PATH = "CONTEXT_MANIFEST.json"


@dataclass(frozen=True, slots=True)
class PersonaInput:
    persona_id: uuid.UUID
    revision: int
    template_version: str
    body: str


@dataclass(frozen=True, slots=True)
class RuleInput:
    rule_id: uuid.UUID
    revision: int
    slug: str
    obligation: str
    body: str


@dataclass(frozen=True, slots=True)
class SkillInput:
    skill_id: uuid.UUID
    revision: int
    slug: str
    when_to_use: str
    body: str
    precedence: int


@dataclass(frozen=True, slots=True)
class MirrorInputs:
    persona: PersonaInput | None
    rules: tuple[RuleInput, ...]
    skills: tuple[SkillInput, ...]


@dataclass(frozen=True, slots=True)
class GeneratedFile:
    path: str
    body: str
    content_sha256: str


@dataclass(frozen=True, slots=True)
class GeneratedMirror:
    files: tuple[GeneratedFile, ...]
    manifest_sha256: str
    input_provenance: dict[str, Any]
    generator_version: str = HARNESS_GENERATOR_VERSION


def slugify(text: str) -> str:
    import re

    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.decode("ascii").lower())
    return slug.strip("-") or "item"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _input_provenance(inputs: MirrorInputs) -> dict[str, Any]:
    return {
        "generator_version": HARNESS_GENERATOR_VERSION,
        "persona": (
            {
                "persona_id": str(inputs.persona.persona_id),
                "revision": inputs.persona.revision,
                "template_version": inputs.persona.template_version,
            }
            if inputs.persona
            else None
        ),
        "rules": [
            {
                "rule_id": str(rule.rule_id),
                "slug": rule.slug,
                "revision": rule.revision,
                "obligation": rule.obligation,
            }
            for rule in sorted(inputs.rules, key=lambda r: (r.obligation,
                                                            r.slug))
        ],
        "skills": [
            {
                "skill_id": str(skill.skill_id),
                "slug": skill.slug,
                "revision": skill.revision,
                "precedence": skill.precedence,
            }
            for skill in sorted(
                inputs.skills, key=lambda s: (s.precedence, s.slug)
            )
        ],
    }


def generate_mirror(inputs: MirrorInputs) -> GeneratedMirror:
    provenance = _input_provenance(inputs)
    content_files: list[GeneratedFile] = []
    if inputs.persona is not None:
        content_files.append(
            GeneratedFile("persona.md", inputs.persona.body,
                          _sha(inputs.persona.body))
        )
    for rule in sorted(inputs.rules, key=lambda r: (r.obligation, r.slug)):
        path = f"rules/{rule.obligation}/{rule.slug}.md"
        content_files.append(GeneratedFile(path, rule.body, _sha(rule.body)))
    if inputs.skills:
        index = {
            "skills": [
                {
                    "slug": skill.slug,
                    "skill_id": str(skill.skill_id),
                    "revision": skill.revision,
                    "precedence": skill.precedence,
                    "when_to_use": skill.when_to_use,
                    "fetch": {
                        "operation_id": "skill.get",
                        "arguments_hint": {
                            "skill_id": str(skill.skill_id),
                            "revision": skill.revision,
                        },
                    },
                }
                for skill in sorted(
                    inputs.skills, key=lambda s: (s.precedence, s.slug)
                )
            ]
        }
        body = json.dumps(index, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":")) + "\n"
        content_files.append(GeneratedFile("skills/INDEX.json", body,
                                           _sha(body)))
    if len(content_files) + 1 > MAX_MIRROR_FILES:
        raise ApiProblem(
            422, "mirror_too_large",
            f"A mirror may not exceed {MAX_MIRROR_FILES} files.",
        )
    files_listing = [
        {"path": f.path, "content_sha256": f.content_sha256,
         "bytes": len(f.body.encode("utf-8"))}
        for f in sorted(content_files, key=lambda f: f.path)
    ]
    files_payload = json.dumps(
        sorted((f["path"], f["content_sha256"]) for f in files_listing),
        separators=(",", ":"),
    ).encode("utf-8")
    manifest_body = json.dumps(
        {
            "generator_version": HARNESS_GENERATOR_VERSION,
            "files_sha256": hashlib.sha256(files_payload).hexdigest(),
            "inputs": provenance,
            "files": files_listing,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ) + "\n"
    manifest = GeneratedFile(MANIFEST_PATH, manifest_body, _sha(manifest_body))
    files = (*sorted(content_files, key=lambda f: f.path), manifest)
    listing = {f.path: f.content_sha256 for f in files}
    manifest_sha256 = _sha(
        "".join(f"{path}:{listing[path]}\n" for path in sorted(listing))
    )
    return GeneratedMirror(
        files=files,
        manifest_sha256=manifest_sha256,
        input_provenance=provenance,
    )


def classify_drift(
    expected: dict[str, str], observed: dict[str, str]
) -> dict[str, Any]:
    classified = []
    drifted = False
    for path in sorted(set(expected) | set(observed)):
        if path not in observed:
            state = "missing"
        elif path not in expected:
            state = "untracked"
        elif observed[path] != expected[path]:
            state = "hand_edited"
        else:
            state = "match"
        if state != "match":
            drifted = True
        classified.append({"path": path, "state": state})
    # A clean mirror reports no file rows; a drifted mirror reports the full
    # classification so the operator sees exactly what matched and what did not.
    return {
        "status": "drifted" if drifted else "clean",
        "files": classified if drifted else [],
    }


def rollback_plan(
    active: dict[str, tuple[str, str]], target: dict[str, tuple[str, str]]
) -> dict[str, Any]:
    restore = [
        {"path": path, "body": body, "content_sha256": digest}
        for path, (body, digest) in sorted(target.items())
        if path not in active or active[path][1] != digest
    ]
    remove = sorted(path for path in active if path not in target)
    return {"restore": restore, "remove": remove}


async def load_mirror_inputs(
    connection: asyncpg.Connection, scope_id: uuid.UUID,
    pins: dict[str, int] | None,
) -> MirrorInputs:
    pins = pins or {}
    persona_pin = pins.get("persona")
    row = await select_persona_revision(connection, scope_id, persona_pin)
    if persona_pin is not None and row is None:
        raise ApiProblem(
            404, "pinned_revision_not_found",
            f"No persona revision {persona_pin} exists in this scope.",
        )
    persona = (
        PersonaInput(
            persona_id=row["persona_id"],
            revision=row["revision"],
            template_version=row["template_version"],
            body=row["body"],
        )
        if row
        else None
    )

    rules: list[RuleInput] = []
    rule_rows = await connection.fetch(
        """
        SELECT rule_id, revision, slug, obligation, body, state
          FROM (
              SELECT DISTINCT ON (rule_id)
                     rule_id, revision, slug, obligation, body, state
                FROM cortex_context.rule_revisions
               WHERE scope_id = $1
               ORDER BY rule_id, revision DESC
          ) AS latest
         WHERE latest.state = 'active'
        """,
        scope_id,
    )
    by_slug = {row["slug"]: row for row in rule_rows}
    all_slugs = set(by_slug)
    for key, revision in pins.items():
        if key.startswith("rule:"):
            all_slugs.add(key.removeprefix("rule:"))
    for slug in sorted(all_slugs):
        pin = pins.get(f"rule:{slug}")
        if pin is None:
            row = by_slug.get(slug)
            if row is None:
                continue
        else:
            row = await connection.fetchrow(
                "SELECT rule_id, revision, slug, obligation, body "
                "FROM cortex_context.rule_revisions "
                "WHERE scope_id = $1 AND slug = $2 AND revision = $3",
                scope_id, slug, pin,
            )
            if row is None:
                raise ApiProblem(
                    404, "pinned_revision_not_found",
                    f"Rule {slug!r} revision {pin} is unavailable.",
                )
        rules.append(
            RuleInput(
                rule_id=row["rule_id"],
                revision=row["revision"],
                slug=row["slug"],
                obligation=row["obligation"],
                body=row["body"],
            )
        )

    skills: list[SkillInput] = []
    skill_rows = await connection.fetch(
        """
        SELECT b.skill_id, b.bound_revision, b.precedence,
               r.slug, r.when_to_use, r.body, r.revision
          FROM cortex_context.skill_bindings AS b
          JOIN cortex_context.skill_revisions AS r
            ON r.scope_id = b.scope_id
           AND r.skill_id = b.skill_id
           AND r.revision = b.bound_revision
         WHERE b.scope_id = $1
         ORDER BY b.precedence, r.slug
        """,
        scope_id,
    )
    for row in skill_rows:
        pin = pins.get(f"skill:{row['slug']}")
        revision = pin if pin is not None else row["revision"]
        if pin is not None and pin != row["revision"]:
            pinned = await connection.fetchrow(
                "SELECT body, when_to_use FROM cortex_context.skill_revisions "
                "WHERE scope_id = $1 AND skill_id = $2 AND revision = $3",
                scope_id, row["skill_id"], pin,
            )
            if pinned is None:
                raise ApiProblem(
                    404, "pinned_revision_not_found",
                    f"Skill {row['slug']!r} revision {pin} is unavailable.",
                )
            body, when_to_use = pinned["body"], pinned["when_to_use"]
        else:
            body, when_to_use = row["body"], row["when_to_use"]
        skills.append(
            SkillInput(
                skill_id=row["skill_id"],
                revision=revision,
                slug=row["slug"],
                when_to_use=when_to_use,
                body=body,
                precedence=row["precedence"],
            )
        )
    return MirrorInputs(persona=persona, rules=tuple(rules),
                        skills=tuple(skills))


async def _load_mirror(
    connection: asyncpg.Connection, context: ScopeContext, label: str
) -> dict[str, Any] | None:
    row = await connection.fetchrow(
        "SELECT mirror_id, label, active_generation FROM "
        "cortex_context.harness_mirrors WHERE scope_id = $1 AND label = $2",
        context.selected.scope_id,
        label,
    )
    return dict(row) if row else None


async def _generation_files(
    connection: asyncpg.Connection, scope_id: uuid.UUID, mirror_id: uuid.UUID,
    generation: int,
) -> dict[str, tuple[str, str]]:
    rows = await connection.fetch(
        "SELECT path, body, content_sha256 FROM "
        "cortex_context.harness_generation_files "
        "WHERE scope_id = $1 AND mirror_id = $2 AND generation = $3",
        scope_id,
        mirror_id,
        generation,
    )
    return {
        row["path"]: (row["body"], row["content_sha256"].hex())
        for row in rows
    }


def _coerce(model: type, payload: Any) -> Any:
    return payload if isinstance(payload, model) else model.model_validate(payload)


async def preview_mirror(
    connection: asyncpg.Connection,
    context: ScopeContext,
    payload: Any,
    path_params: dict[str, Any],
) -> dict[str, Any]:
    request = _coerce(HarnessPreviewRequest, payload)
    inputs = await load_mirror_inputs(
        connection, context.selected.scope_id, request.pins
    )
    mirror = generate_mirror(inputs)
    return {
        "mirror_label": request.mirror_label,
        "generator_version": mirror.generator_version,
        "manifest_sha256": mirror.manifest_sha256,
        "input_provenance": mirror.input_provenance,
        "files": [
            {"path": f.path, "content_sha256": f.content_sha256,
             "bytes": len(f.body.encode("utf-8")), "body": f.body}
            for f in mirror.files
        ],
    }


async def apply_mirror(
    connection: asyncpg.Connection,
    context: ScopeContext,
    idempotency_key: str,
    payload: Any,
    path_params: dict[str, Any],
) -> tuple[int, dict[str, Any], bool]:
    request = _coerce(HarnessApplyRequest, payload)
    operation = "harness.apply"
    digest = request_digest(
        {
            "operation": operation,
            "scope_id": str(context.selected.scope_id),
            "mirror_label": request.mirror_label,
            "pins": dict(sorted(request.pins.items())),
            "observed_manifest": (
                dict(sorted(request.observed_manifest.items()))
                if request.observed_manifest is not None
                else None
            ),
        }
    )
    previous, replayed = await begin_command(
        connection,
        principal_id=context.principal.principal_id,
        operation=operation,
        idempotency_key=idempotency_key,
        digest=digest,
        scope_id=context.selected.scope_id,
    )
    if replayed and previous is not None:
        return 200, previous, True
    policy_revision, _ = await declare_policy_revision(connection, context)

    mirror = await _load_mirror(connection, context, request.mirror_label)
    if mirror is not None and mirror["active_generation"] is not None:
        if request.observed_manifest is None:
            raise ApiProblem(
                422, "observed_manifest_required",
                "Applying over an existing mirror requires the host's "
                "observed manifest so hand edits are detected.",
            )
        active_files = await _generation_files(
            connection, context.selected.scope_id, mirror["mirror_id"],
            mirror["active_generation"],
        )
        expected = {path: digest_ for path, (_b, digest_) in
                    active_files.items()}
        report = classify_drift(expected, dict(request.observed_manifest))
        if report["status"] == "drifted":
            detail = ", ".join(
                f"{item['path']} ({item['state']})"
                for item in report["files"][:10]
            )
            raise ApiProblem(
                409, "harness_drift_detected",
                "The mirror on the host does not match the active "
                f"generation: {detail}. Roll back or reconcile before "
                "applying.",
            )
        mirror_id = mirror["mirror_id"]
        generation = int(mirror["active_generation"]) + 1
    else:
        mirror_id = mirror["mirror_id"] if mirror else uuid.uuid4()
        generation = 1
        if mirror is None:
            await connection.execute(
                """
                INSERT INTO cortex_context.harness_mirrors
                    (scope_id, mirror_id, label, active_generation,
                     created_by_principal)
                VALUES ($1, $2, $3, NULL, $4)
                """,
                context.selected.scope_id,
                mirror_id,
                request.mirror_label,
                context.principal.principal_id,
            )

    inputs = await load_mirror_inputs(
        connection, context.selected.scope_id, request.pins
    )
    generated = generate_mirror(inputs)
    await connection.execute(
        """
        INSERT INTO cortex_context.harness_generations
            (scope_id, mirror_id, generation, generator_version,
             input_provenance, manifest_sha256, file_count,
             created_by_principal)
        VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8)
        """,
        context.selected.scope_id,
        mirror_id,
        generation,
        generated.generator_version,
        json.dumps(generated.input_provenance, ensure_ascii=False,
                   sort_keys=True),
        bytes.fromhex(generated.manifest_sha256),
        len(generated.files),
        context.principal.principal_id,
    )
    for file in generated.files:
        await connection.execute(
            """
            INSERT INTO cortex_context.harness_generation_files
                (scope_id, mirror_id, generation, path, body, content_sha256)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            context.selected.scope_id,
            mirror_id,
            generation,
            file.path,
            file.body,
            bytes.fromhex(file.content_sha256),
        )
    await connection.execute(
        "UPDATE cortex_context.harness_mirrors SET active_generation = $3, "
        "updated_at = now() WHERE scope_id = $1 AND mirror_id = $2",
        context.selected.scope_id,
        mirror_id,
        generation,
    )
    receipt = {
        "state": "committed",
        "operation": operation,
        "mirror_id": str(mirror_id),
        "mirror_label": request.mirror_label,
        "generation": generation,
        "manifest_sha256": generated.manifest_sha256,
        "scope_id": str(context.selected.scope_id),
        "policy_revision": policy_revision,
        "input_provenance": generated.input_provenance,
        "files": [
            {"path": f.path, "content_sha256": f.content_sha256,
             "bytes": len(f.body.encode("utf-8"))}
            for f in generated.files
        ],
    }
    await commit_receipt(
        connection,
        principal_id=context.principal.principal_id,
        operation=operation,
        idempotency_key=idempotency_key,
        digest=digest,
        receipt_kind="committed",
        receipt=receipt,
        scope_id=context.selected.scope_id,
    )
    return 201, receipt, False


async def drift_check(
    connection: asyncpg.Connection,
    context: ScopeContext,
    payload: Any,
    path_params: dict[str, Any],
) -> dict[str, Any]:
    request = _coerce(HarnessDriftRequest, payload)
    mirror = await _load_mirror(connection, context, request.mirror_label)
    if mirror is None or mirror["active_generation"] is None:
        raise ApiProblem(
            404, "mirror_not_found",
            "The harness mirror is unavailable in the selected scope.",
        )
    active_files = await _generation_files(
        connection, context.selected.scope_id, mirror["mirror_id"],
        mirror["active_generation"],
    )
    expected = {path: digest for path, (_b, digest) in active_files.items()}
    report = classify_drift(expected, dict(request.observed_manifest))
    generation = await connection.fetchrow(
        "SELECT manifest_sha256 FROM cortex_context.harness_generations "
        "WHERE scope_id = $1 AND mirror_id = $2 AND generation = $3",
        context.selected.scope_id,
        mirror["mirror_id"],
        mirror["active_generation"],
    )
    return {
        "mirror_label": request.mirror_label,
        "mirror_id": str(mirror["mirror_id"]),
        "generation": mirror["active_generation"],
        "manifest_sha256": generation["manifest_sha256"].hex(),
        "status": report["status"],
        "files": report["files"],
    }


async def rollback_mirror(
    connection: asyncpg.Connection,
    context: ScopeContext,
    idempotency_key: str,
    payload: Any,
    path_params: dict[str, Any],
) -> tuple[int, dict[str, Any], bool]:
    request = _coerce(HarnessRollbackRequest, payload)
    operation = "harness.rollback"
    digest = request_digest(
        {
            "operation": operation,
            "scope_id": str(context.selected.scope_id),
            "mirror_label": request.mirror_label,
            "target_generation": request.target_generation,
        }
    )
    previous, replayed = await begin_command(
        connection,
        principal_id=context.principal.principal_id,
        operation=operation,
        idempotency_key=idempotency_key,
        digest=digest,
        scope_id=context.selected.scope_id,
    )
    if replayed and previous is not None:
        return 200, previous, True
    policy_revision, _ = await declare_policy_revision(connection, context)

    mirror = await _load_mirror(connection, context, request.mirror_label)
    if mirror is None or mirror["active_generation"] is None:
        raise ApiProblem(
            404, "mirror_not_found",
            "The harness mirror is unavailable in the selected scope.",
        )
    active_generation = int(mirror["active_generation"])
    if request.target_generation is not None:
        target = request.target_generation
        if target < 1 or target > active_generation:
            raise ApiProblem(
                422, "no_rollback_target",
                "target_generation must name an existing applied generation "
                "(1..active).",
            )
    elif active_generation > 1:
        target = active_generation - 1
    else:
        target = active_generation
    if target == active_generation and request.observed_manifest is None:
        raise ApiProblem(
            422, "observed_manifest_required",
            "Rolling back onto the active generation repairs host drift and "
            "requires the host's observed manifest.",
        )
    target_row = await connection.fetchrow(
        "SELECT manifest_sha256, generator_version, input_provenance "
        "FROM cortex_context.harness_generations "
        "WHERE scope_id = $1 AND mirror_id = $2 AND generation = $3",
        context.selected.scope_id,
        mirror["mirror_id"],
        target,
    )
    if target_row is None:
        raise ApiProblem(
            404, "generation_not_found",
            f"Generation {target} does not exist for this mirror.",
        )
    active_files = await _generation_files(
        connection, context.selected.scope_id, mirror["mirror_id"],
        active_generation,
    )
    target_files = await _generation_files(
        connection, context.selected.scope_id, mirror["mirror_id"], target
    )
    if request.observed_manifest is not None:
        observed = dict(request.observed_manifest)
        restore = [
            {"path": path, "body": body, "content_sha256": digest}
            for path, (body, digest) in sorted(target_files.items())
            if observed.get(path) != digest
        ]
        remove = sorted(path for path in observed if path not in target_files)
        plan = {"restore": restore, "remove": remove}
    else:
        plan = rollback_plan(active_files, target_files)

    new_generation = active_generation + 1
    provenance = json.loads(target_row["input_provenance"]) \
        if isinstance(target_row["input_provenance"], str) \
        else dict(target_row["input_provenance"])
    provenance.update(
        {
            "rolled_back_from": active_generation,
            "rolled_back_to": target,
        }
    )
    await connection.execute(
        """
        INSERT INTO cortex_context.harness_generations
            (scope_id, mirror_id, generation, generator_version,
             input_provenance, manifest_sha256, file_count,
             created_by_principal)
        VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8)
        """,
        context.selected.scope_id,
        mirror["mirror_id"],
        new_generation,
        target_row["generator_version"],
        json.dumps(provenance, ensure_ascii=False, sort_keys=True),
        target_row["manifest_sha256"],
        len(target_files),
        context.principal.principal_id,
    )
    for path, (body, file_digest) in sorted(target_files.items()):
        await connection.execute(
            """
            INSERT INTO cortex_context.harness_generation_files
                (scope_id, mirror_id, generation, path, body, content_sha256)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            context.selected.scope_id,
            mirror["mirror_id"],
            new_generation,
            path,
            body,
            bytes.fromhex(file_digest),
        )
    await connection.execute(
        "UPDATE cortex_context.harness_mirrors SET active_generation = $3, "
        "updated_at = now() WHERE scope_id = $1 AND mirror_id = $2",
        context.selected.scope_id,
        mirror["mirror_id"],
        new_generation,
    )
    receipt = {
        "state": "committed",
        "operation": operation,
        "mirror_id": str(mirror["mirror_id"]),
        "mirror_label": request.mirror_label,
        "generation": new_generation,
        "rolled_back_from": active_generation,
        "rolled_back_to": target,
        "manifest_sha256": target_row["manifest_sha256"].hex(),
        "scope_id": str(context.selected.scope_id),
        "policy_revision": policy_revision,
        "restore": plan["restore"],
        "remove": plan["remove"],
    }
    await commit_receipt(
        connection,
        principal_id=context.principal.principal_id,
        operation=operation,
        idempotency_key=idempotency_key,
        digest=digest,
        receipt_kind="committed",
        receipt=receipt,
        scope_id=context.selected.scope_id,
    )
    return 201, receipt, False


__all__ = [
    "HARNESS_GENERATOR_VERSION",
    "GeneratedFile",
    "GeneratedMirror",
    "MirrorInputs",
    "PersonaInput",
    "RuleInput",
    "SkillInput",
    "apply_mirror",
    "classify_drift",
    "drift_check",
    "generate_mirror",
    "load_mirror_inputs",
    "preview_mirror",
    "rollback_mirror",
    "rollback_plan",
    "slugify",
]
