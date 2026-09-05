#!/usr/bin/env python3

import argparse
import contextlib
import datetime as dt
import io
import json
import pathlib
import sys
from collections import Counter


def sql_escape(value: str) -> str:
    return value.replace("'", "''")


def iso_utc(value):
    if value in (None, "", 0):
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return (
            dt.datetime.fromtimestamp(float(value) / 1000.0, tz=dt.timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
    return None


def safe_int(value):
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize(value):
    value_type = type(value).__name__
    if value_type in {"Null", "Undefined"}:
        return None
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(key): normalize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [normalize(item) for item in value]
    if hasattr(value, "values") and hasattr(value, "properties"):
        values = list(getattr(value, "values", []))
        properties = dict(getattr(value, "properties", {}))
        int_keys = [key for key in properties if isinstance(key, int)]
        if len(int_keys) == len(properties):
            max_index = max([-1, len(values) - 1] + int_keys)
            items = []
            for index in range(max_index + 1):
                if index in properties:
                    item = properties[index]
                elif index < len(values):
                    item = values[index]
                else:
                    item = None
                items.append(normalize(item))
            return items
        normalized = {str(key): normalize(item) for key, item in properties.items()}
        if values:
            normalized["_values"] = [normalize(item) for item in values]
        return normalized
    if hasattr(value, "__dict__"):
        return {str(key): normalize(item) for key, item in vars(value).items()}
    return str(value)


def extract_tiptap_text(node):
    if isinstance(node, list):
        return "".join(extract_tiptap_text(item) for item in node)
    if not isinstance(node, dict):
        return ""

    parts = []
    text = node.get("text")
    if isinstance(text, str):
        parts.append(text)
    for child in node.get("content") or []:
        parts.append(extract_tiptap_text(child))

    rendered = "".join(parts)
    if node.get("type") in {
        "paragraph",
        "heading",
        "blockquote",
        "listItem",
        "orderedList",
        "bulletList",
        "codeBlock",
    }:
        rendered = rendered.rstrip() + "\n"
    return rendered


def basename_no_ext(path: pathlib.Path) -> str:
    return path.name.rsplit(".", 1)[0]


def build_todo_content(source_file: pathlib.Path, items):
    lines = [
        "# Claude Todo Snapshot",
        "",
        f"Source: {source_file}",
        "",
        f"Items: {len(items)}",
        "",
    ]
    for item in items:
        status = str(item.get("status") or "pending").strip() or "pending"
        content = str(item.get("content") or "(empty)").strip() or "(empty)"
        lines.append(f"- [{status}] {content}")
        active_form = str(item.get("activeForm") or "").strip()
        if active_form:
            lines.append(f"  active: {active_form}")
    return "\n".join(lines).rstrip() + "\n"


def build_account_content(account):
    lines = [
        f"Account UUID: {account['uuid']}",
    ]
    if account.get("tagged_id"):
        lines.append(f"Tagged ID: {account['tagged_id']}")
    if account.get("full_name"):
        lines.append(f"Full Name: {account['full_name']}")
    if account.get("display_name"):
        lines.append(f"Display Name: {account['display_name']}")
    if account.get("email_address"):
        lines.append(f"Email: {account['email_address']}")
    if account.get("sources"):
        lines.append(f"Sources: {', '.join(sorted(account['sources']))}")
    lines.append("")
    lines.append("Memberships:")
    memberships = sorted(
        account.get("memberships", {}).values(),
        key=lambda item: (
            str(item.get("organization_name") or ""),
            str(item.get("org_uuid") or ""),
        ),
    )
    if not memberships:
        lines.append("- (none)")
    for membership in memberships:
        capabilities = ", ".join(membership.get("capabilities") or []) or "(none)"
        lines.append(
            "- "
            f"{membership.get('organization_name') or '(unnamed)'}"
            f" | role={membership.get('role') or '(none)'}"
            f" | org_uuid={membership.get('org_uuid') or '(none)'}"
            f" | billing_type={membership.get('billing_type') or '(none)'}"
            f" | merchant_of_record={membership.get('merchant_of_record') or '(none)'}"
            f" | capabilities={capabilities}"
        )
    return "\n".join(lines).rstrip() + "\n"


def build_conversation_content(conversation):
    title = str(conversation.get("name") or "").strip() or "(untitled)"
    lines = [
        f"Conversation UUID: {conversation['uuid']}",
        f"Title: {title}",
    ]
    if conversation.get("model"):
        lines.append(f"Model: {conversation['model']}")
    if conversation.get("created_at"):
        lines.append(f"Created At: {conversation['created_at']}")
    if conversation.get("updated_at"):
        lines.append(f"Updated At: {conversation['updated_at']}")
    if conversation.get("project_name"):
        lines.append(f"Project: {conversation['project_name']}")
    if conversation.get("project_uuid"):
        lines.append(f"Project UUID: {conversation['project_uuid']}")
    if conversation.get("org_uuid"):
        lines.append(f"Org UUID: {conversation['org_uuid']}")
    if conversation.get("platform"):
        lines.append(f"Platform: {conversation['platform']}")
    lines.append(f"Starred: {bool(conversation.get('is_starred'))}")
    lines.append(f"Temporary: {bool(conversation.get('is_temporary'))}")
    if conversation.get("current_leaf_message_uuid"):
        lines.append(
            f"Current Leaf Message UUID: {conversation['current_leaf_message_uuid']}"
        )
    if conversation.get("sources"):
        lines.append(f"Sources: {', '.join(sorted(conversation['sources']))}")

    summary = str(conversation.get("summary") or "").strip()
    if summary:
        lines.extend(["", "Summary:", summary])
    return "\n".join(lines).rstrip() + "\n"


def build_draft_content(draft):
    sources = [str(source) for source in (draft.get("sources") or []) if source not in (None, "")]
    source_label = sources[0] if len(sources) == 1 else ", ".join(sources) or "(none)"
    source_header = "Source" if len(sources) == 1 else "Sources"
    draft_text = str(draft.get("text") or "").strip() or "(empty)"
    lines = [
        f"Draft Key: {draft['draft_key']}",
        f"Draft ID: {draft['draft_id']}",
        f"{source_header}: {source_label}",
    ]
    if draft.get("updated_at"):
        lines.append(f"Updated At: {draft['updated_at']}")
    lines.append(f"Attachment Count: {draft.get('attachment_count', 0)}")
    lines.append(f"File Count: {draft.get('file_count', 0)}")
    lines.extend(["", "Draft Text:", draft_text])
    return "\n".join(lines).rstrip() + "\n"


def build_source_content(source):
    lines = [
        f"Source ID: {source['source_id']}",
        f"Kind: {source['kind']}",
        f"LevelDB Dir: {source['leveldb_dir']}",
        f"Blob Dir: {source['blob_dir'] or '(none)'}",
        f"Conversations Extracted: {source['conversation_count']}",
        f"Drafts Extracted: {source['draft_count']}",
        "",
        "Databases:",
    ]
    if not source["databases"]:
        lines.append("- (none)")
    for database in source["databases"]:
        stores = ", ".join(database["stores"]) if database["stores"] else "(none)"
        lines.append(
            f"- {database['name']} v{database['version']}: {stores} ({len(database['stores'])})"
        )
    lines.extend(["", "Query Keys:"])
    if source["query_keys"]:
        for key, count in sorted(source["query_keys"].items()):
            lines.append(f"- {key}: {count}")
    else:
        lines.append("- (none)")
    lines.extend(["", "Errors:"])
    if source["errors"]:
        for error in source["errors"]:
            lines.append(f"- {error}")
    else:
        lines.append("- (none)")
    return "\n".join(lines).rstrip() + "\n"


def row_to_sql(row):
    content = sql_escape(row["content"])
    source_file = sql_escape(row["source_file"])
    category = sql_escape(row["category"])
    section = sql_escape(row["section"])
    project = sql_escape(row["project"])
    project_id = (
        "NULL"
        if row["project"].startswith("_")
        else f"(SELECT id FROM cortex_projects WHERE project_key = '{project}')"
    )
    return (
        f"DELETE FROM knowledge WHERE project = '{project}' "
        f"AND category = '{category}' AND source_file = '{source_file}';\n"
        "INSERT INTO knowledge (content, source_file, category, section, project, project_id, updated_at) "
        f"VALUES ('{content}', '{source_file}', '{category}', '{section}', '{project}', {project_id}, NOW());"
    )


def discover_todos() -> list[pathlib.Path]:
    todos_dir = pathlib.Path.home() / ".claude" / "todos"
    if not todos_dir.is_dir():
        return []
    return sorted(path for path in todos_dir.glob("*.json") if path.is_file())


def discover_plans() -> list[pathlib.Path]:
    plans_dir = pathlib.Path.home() / ".claude" / "plans"
    if not plans_dir.is_dir():
        return []
    return sorted(path for path in plans_dir.glob("*.md") if path.is_file())


def discover_indexeddb_dirs() -> list[pathlib.Path]:
    home = pathlib.Path.home()
    patterns = [
        home / "Library/Application Support/Claude/IndexedDB/https_claude.ai_0.indexeddb.leveldb",
        home / "Library/Application Support/Google/Chrome/Default/IndexedDB/https_claude.ai_0.indexeddb.leveldb",
        home / "Library/Application Support/BraveSoftware/Brave-Browser/Default/IndexedDB/https_claude.ai_0.indexeddb.leveldb",
    ]
    arc_root = home / "Library/Application Support/Arc/User Data"
    if arc_root.is_dir():
        patterns.extend(sorted(arc_root.glob("*/IndexedDB/https_claude.ai_0.indexeddb.leveldb")))
    seen = set()
    discovered = []
    for path in patterns:
        real = path.expanduser().resolve()
        if real.is_dir() and real not in seen:
            seen.add(real)
            discovered.append(real)
    return sorted(discovered)


def classify_source(leveldb_dir: pathlib.Path) -> tuple[str, str]:
    path = str(leveldb_dir)
    if "/Application Support/Claude/IndexedDB/" in path:
        return "claude-desktop", "desktop"
    if "/Google/Chrome/Default/" in path:
        return "chrome-default", "chrome"
    if "/BraveSoftware/Brave-Browser/Default/" in path:
        return "brave-default", "brave"
    if "/Arc/User Data/Default/" in path:
        return "arc-default", "arc"
    parts = leveldb_dir.parts
    if "Arc" in parts and "User Data" in parts:
        for index, part in enumerate(parts):
            if part == "User Data" and index + 1 < len(parts):
                profile = parts[index + 1]
                if profile.startswith("Profile "):
                    suffix = profile.split(" ", 1)[1].strip().lower().replace(" ", "-")
                    return f"arc-profile-{suffix}", "arc"
    return leveldb_dir.name.replace(".indexeddb.leveldb", ""), "unknown"


def blob_file_path(blob_dir: pathlib.Path, database_id: int, blob_number: int) -> pathlib.Path:
    return blob_dir / str(database_id) / f"{blob_number >> 8:02x}" / format(blob_number, "x")


def load_indexeddb_modules(vendor_dir: str):
    if vendor_dir:
        sys.path.insert(0, vendor_dir)
    from dfindexeddb.leveldb import record as leveldb_record
    from dfindexeddb.indexeddb.chromium import blink
    from dfindexeddb.indexeddb.chromium import record as chromium_record

    return leveldb_record, chromium_record, blink


def choose_latest_record(current, candidate):
    if current is None:
        return candidate
    current_version = getattr(current.value, "version", 0) or 0
    candidate_version = getattr(candidate.value, "version", 0) or 0
    if candidate_version > current_version:
        return candidate
    current_sequence = getattr(current, "sequence_number", 0) or 0
    candidate_sequence = getattr(candidate, "sequence_number", 0) or 0
    if candidate_sequence > current_sequence:
        return candidate
    return current


def scan_source(leveldb_dir: pathlib.Path, leveldb_record, chromium_record, blink):
    source_id, kind = classify_source(leveldb_dir)
    blob_dir = pathlib.Path(str(leveldb_dir).replace(".leveldb", ".blob"))
    has_blob_dir = blob_dir.is_dir()

    db_names = {}
    db_versions = {}
    store_names = {}
    direct_records = {}
    blob_records = {}
    query_keys = Counter()
    conversations = {}
    drafts = {}
    accounts = {}
    errors = []

    reader = leveldb_record.FolderReader(leveldb_dir)
    stderr_buffer = io.StringIO()
    with contextlib.redirect_stderr(stderr_buffer):
        for db_record in reader.GetRecords():
            try:
                record = chromium_record.ChromiumIndexedDBRecord.FromLevelDBRecord(
                    db_record,
                    parse_value=True,
                    include_raw_data=False,
                    blob_folder_reader=None,
                    load_blobs=False,
                )
            except Exception:
                continue

            key_type = type(record.key).__name__
            if key_type == "DatabaseNameKey" and record.value is not None:
                try:
                    db_names[int(record.value)] = record.key.database_name
                except Exception:
                    pass
                continue

            if key_type == "DatabaseMetaDataKey":
                metadata_name = getattr(getattr(record.key, "metadata_type", None), "name", "")
                if metadata_name == "IDB_INTEGER_VERSION":
                    database_id = safe_int(getattr(record.key.key_prefix, "database_id", None))
                    database_version = safe_int(record.value)
                    if database_id is not None and database_version is not None:
                        db_versions[database_id] = database_version
                continue

            if key_type == "ObjectStoreMetaDataKey":
                metadata_name = getattr(getattr(record.key, "metadata_type", None), "name", "")
                if metadata_name == "OBJECT_STORE_NAME" and record.value not in (None, ""):
                    database_id = safe_int(getattr(record.key.key_prefix, "database_id", None))
                    object_store_id = safe_int(getattr(record.key, "object_store_id", None))
                    if database_id is None or object_store_id is None:
                        continue
                    store_names[
                        (database_id, object_store_id)
                    ] = str(record.value)
                continue

            if key_type == "ObjectStoreDataKey":
                user_key = getattr(record.key.encoded_user_key, "value", None)
                if isinstance(user_key, str):
                    map_key = (
                        int(record.database_id),
                        int(record.object_store_id),
                        user_key,
                    )
                    direct_records[map_key] = choose_latest_record(direct_records.get(map_key), record)
                continue

            if key_type == "BlobEntryKey":
                user_key = getattr(record.key.user_key, "value", None)
                entries = getattr(record.value, "entries", None) or []
                if not isinstance(user_key, str):
                    continue
                database_id = safe_int(getattr(record, "database_id", None))
                object_store_id = safe_int(getattr(record, "object_store_id", None))
                if database_id is None or object_store_id is None:
                    continue
                map_key = (
                    database_id,
                    object_store_id,
                    user_key,
                )
                bucket = blob_records.setdefault(map_key, [])
                for entry in entries:
                    blob_number = safe_int(getattr(entry, "blob_number", None))
                    blob_size = safe_int(getattr(entry, "size", None))
                    if blob_number is None:
                        continue
                    bucket.append(
                        {
                            "blob_number": blob_number,
                            "size": blob_size or 0,
                            "mime_type": str(entry.mime_type),
                        }
                    )

    react_query_cache = None
    react_key = next(
        (
            key
            for key in direct_records
            if key[2] == "react-query-cache"
        ),
        None,
    )
    if react_key:
        inline_value = getattr(direct_records[react_key].value, "value", None)
        if inline_value is not None:
            react_query_cache = normalize(inline_value)
        elif has_blob_dir:
            candidates = sorted(
                blob_records.get(react_key, []),
                key=lambda item: item["blob_number"],
                reverse=True,
            )
            chosen_blob = None
            for candidate in candidates:
                blob_path = blob_file_path(blob_dir, react_key[0], candidate["blob_number"])
                if blob_path.is_file():
                    chosen_blob = blob_path
                    break
            if chosen_blob:
                try:
                    react_query_cache = normalize(
                        blink.V8ScriptValueDecoder.FromBytes(chosen_blob.read_bytes())
                    )
                except Exception as exc:
                    errors.append(f"Failed to decode react-query-cache blob: {exc}")
            elif candidates:
                errors.append("No readable react-query-cache blob was found")
        else:
            errors.append("Blob dir missing for react-query-cache")

    if react_query_cache:
        queries = (
            react_query_cache.get("clientState", {}).get("queries", [])
            if isinstance(react_query_cache, dict)
            else []
        )
        if isinstance(queries, list):
            for query in queries:
                if not isinstance(query, dict):
                    continue
                query_key = query.get("queryKey") or []
                query_name = query_key[0] if isinstance(query_key, list) and query_key else None
                if query_name:
                    query_keys[str(query_name)] += 1

                if query_name == "current_account":
                    account_data = (
                        query.get("state", {})
                        .get("data", {})
                        .get("account")
                    )
                    if isinstance(account_data, dict) and account_data.get("uuid"):
                        account = accounts.setdefault(
                            str(account_data["uuid"]),
                            {
                                "uuid": str(account_data["uuid"]),
                                "tagged_id": account_data.get("tagged_id"),
                                "full_name": account_data.get("full_name"),
                                "display_name": account_data.get("display_name"),
                                "email_address": account_data.get("email_address"),
                                "sources": set(),
                                "memberships": {},
                            },
                        )
                        account["sources"].add(source_id)
                        for membership in account_data.get("memberships") or []:
                            if not isinstance(membership, dict):
                                continue
                            organization = membership.get("organization") or {}
                            org_uuid = organization.get("uuid")
                            if not org_uuid:
                                continue
                            account["memberships"][str(org_uuid)] = {
                                "organization_name": organization.get("name"),
                                "org_uuid": str(org_uuid),
                                "role": membership.get("role"),
                                "billing_type": organization.get("billing_type"),
                                "merchant_of_record": organization.get("merchant_of_record"),
                                "capabilities": [
                                    str(cap)
                                    for cap in (organization.get("capabilities") or [])
                                    if cap not in (None, "")
                                ],
                            }

                if query_name == "chat_conversation_list":
                    org_uuid = None
                    if isinstance(query_key, list) and len(query_key) > 1 and isinstance(query_key[1], dict):
                        org_uuid = query_key[1].get("orgUuid")
                    items = (
                        query.get("state", {})
                        .get("data", {})
                        .get("data", [])
                    )
                    if not isinstance(items, list):
                        continue
                    for item in items:
                        if not isinstance(item, dict) or not item.get("uuid"):
                            continue
                        conversation = conversations.setdefault(
                            str(item["uuid"]),
                            {
                                "uuid": str(item["uuid"]),
                                "name": item.get("name"),
                                "summary": item.get("summary"),
                                "model": item.get("model"),
                                "created_at": item.get("created_at"),
                                "updated_at": item.get("updated_at"),
                                "project_uuid": item.get("project_uuid") or (item.get("project") or {}).get("uuid"),
                                "project_name": (item.get("project") or {}).get("name"),
                                "org_uuid": org_uuid,
                                "platform": item.get("platform"),
                                "is_starred": bool(item.get("is_starred") or item.get("starred")),
                                "is_temporary": bool(item.get("is_temporary") or item.get("temporary")),
                                "current_leaf_message_uuid": item.get("current_leaf_message_uuid"),
                                "sources": set(),
                            },
                        )
                        conversation["sources"].add(source_id)
                        updated_at = item.get("updated_at")
                        current_updated = conversation.get("updated_at")
                        if updated_at and (
                            not current_updated or str(updated_at) > str(current_updated)
                        ):
                            conversation.update(
                                {
                                    "name": item.get("name"),
                                    "summary": item.get("summary"),
                                    "model": item.get("model"),
                                    "created_at": item.get("created_at"),
                                    "updated_at": updated_at,
                                    "project_uuid": item.get("project_uuid")
                                    or (item.get("project") or {}).get("uuid"),
                                    "project_name": (item.get("project") or {}).get("name"),
                                    "org_uuid": org_uuid,
                                    "platform": item.get("platform"),
                                    "is_starred": bool(item.get("is_starred") or item.get("starred")),
                                    "is_temporary": bool(item.get("is_temporary") or item.get("temporary")),
                                    "current_leaf_message_uuid": item.get("current_leaf_message_uuid"),
                                }
                            )

    for (_, _, user_key), record in direct_records.items():
        if not user_key.startswith("store:chat-draft:"):
            continue
        draft_id = user_key.split("store:chat-draft:", 1)[1]
        raw_value = getattr(record.value, "value", "")
        if not isinstance(raw_value, str):
            continue
        try:
            payload = json.loads(raw_value)
        except json.JSONDecodeError:
            errors.append(f"Failed to parse draft payload for {draft_id}")
            continue
        state = payload.get("state") or {}
        draft_text = extract_tiptap_text((state.get("tipTapEditorState") or {}).get("content") or [])
        drafts[draft_id] = {
            "draft_key": user_key,
            "draft_id": draft_id,
            "updated_at": iso_utc(payload.get("updatedAt") or payload.get("updated_at")),
            "attachment_count": len(state.get("attachments") or []),
            "file_count": len(state.get("files") or []),
            "text": draft_text.strip(),
            "sources": [source_id],
        }

    databases = []
    all_db_ids = sorted(set(db_names.keys()) | set(db_versions.keys()))
    for database_id in all_db_ids:
        name = db_names.get(database_id)
        if not name:
            continue
        stores = [
            store_name
            for (db_id, _), store_name in sorted(store_names.items())
            if db_id == database_id
        ]
        databases.append(
            {
                "name": name,
                "version": db_versions.get(database_id, 1),
                "stores": stores,
            }
        )

    return {
        "source_id": source_id,
        "kind": kind,
        "leveldb_dir": str(leveldb_dir),
        "blob_dir": str(blob_dir) if has_blob_dir else None,
        "databases": databases,
        "query_keys": dict(query_keys),
        "errors": errors,
        "accounts": accounts,
        "conversations": conversations,
        "drafts": drafts,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-sql", required=True)
    parser.add_argument("--project", default="_global")
    parser.add_argument("--vendor-dir", default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-todos", action="store_true")
    parser.add_argument("--skip-plans", action="store_true")
    parser.add_argument("--skip-indexeddb", action="store_true")
    args = parser.parse_args()

    rows = []
    summary = {
        "todos": 0,
        "plans": 0,
        "indexeddb_sources": 0,
        "indexeddb_accounts": 0,
        "indexeddb_conversations": 0,
        "indexeddb_drafts": 0,
    }

    if not args.skip_todos:
        for todo_file in discover_todos():
            try:
                items = json.loads(todo_file.read_text())
            except Exception:
                continue
            if not isinstance(items, list):
                continue
            rows.append(
                {
                    "project": args.project,
                    "category": "claude-todo",
                    "source_file": str(todo_file),
                    "section": basename_no_ext(todo_file),
                    "content": build_todo_content(todo_file, items),
                }
            )
            summary["todos"] += 1

    if not args.skip_plans:
        for plan_file in discover_plans():
            try:
                content = plan_file.read_text()
            except Exception:
                continue
            if not content.strip():
                continue
            rows.append(
                {
                    "project": args.project,
                    "category": "claude-plan",
                    "source_file": str(plan_file),
                    "section": basename_no_ext(plan_file),
                    "content": content.rstrip() + "\n",
                }
            )
            summary["plans"] += 1

    if not args.skip_indexeddb:
        leveldb_record, chromium_record, blink = load_indexeddb_modules(args.vendor_dir)
        accounts = {}
        conversations = {}
        drafts = {}
        for leveldb_dir in discover_indexeddb_dirs():
            source = scan_source(leveldb_dir, leveldb_record, chromium_record, blink)
            rows.append(
                {
                    "project": args.project,
                    "category": "claude-indexeddb-source",
                    "source_file": f"claude-indexeddb://source/{source['source_id']}",
                    "section": source["source_id"],
                    "content": build_source_content(
                        {
                            **source,
                            "conversation_count": len(source["conversations"]),
                            "draft_count": len(source["drafts"]),
                        }
                    ),
                }
            )
            summary["indexeddb_sources"] += 1

            for account_uuid, account in source["accounts"].items():
                aggregate = accounts.setdefault(
                    account_uuid,
                    {
                        "uuid": account_uuid,
                        "tagged_id": account.get("tagged_id"),
                        "full_name": account.get("full_name"),
                        "display_name": account.get("display_name"),
                        "email_address": account.get("email_address"),
                        "sources": set(),
                        "memberships": {},
                    },
                )
                aggregate["sources"].update(account.get("sources") or set())
                aggregate["memberships"].update(account.get("memberships") or {})

            for conversation_uuid, conversation in source["conversations"].items():
                aggregate = conversations.setdefault(
                    conversation_uuid,
                    {
                        **conversation,
                        "sources": set(),
                    },
                )
                aggregate["sources"].update(conversation.get("sources") or set())
                updated_at = conversation.get("updated_at")
                current_updated = aggregate.get("updated_at")
                if updated_at and (not current_updated or str(updated_at) > str(current_updated)):
                    aggregate.update({k: v for k, v in conversation.items() if k != "sources"})

            for draft_id, draft in source["drafts"].items():
                source_key = f"{source['source_id']}:{draft_id}"
                drafts[source_key] = draft

        for account_uuid, account in sorted(accounts.items()):
            rows.append(
                {
                    "project": args.project,
                    "category": "claude-indexeddb-account",
                    "source_file": f"claude-indexeddb://account/{account_uuid}",
                    "section": str(account.get("display_name") or account_uuid),
                    "content": build_account_content(account),
                }
            )
            summary["indexeddb_accounts"] += 1

        for conversation_uuid, conversation in sorted(conversations.items()):
            rows.append(
                {
                    "project": args.project,
                    "category": "claude-indexeddb-conversation",
                    "source_file": f"claude-indexeddb://conversation/{conversation_uuid}",
                    "section": str(conversation.get("name") or "(untitled)"),
                    "content": build_conversation_content(conversation),
                }
            )
            summary["indexeddb_conversations"] += 1

        for source_key, draft in sorted(drafts.items()):
            rows.append(
                {
                    "project": args.project,
                    "category": "claude-indexeddb-draft",
                    "source_file": f"claude-indexeddb://draft/{source_key}",
                    "section": draft["draft_id"],
                    "content": build_draft_content(draft),
                }
            )
            summary["indexeddb_drafts"] += 1

    sql_lines = ["BEGIN;"]
    if not args.dry_run:
        sql_lines.extend(row_to_sql(row) for row in rows)
    sql_lines.append("COMMIT;")
    pathlib.Path(args.output_sql).write_text("\n".join(sql_lines) + "\n")

    print(f"Todos imported:            {summary['todos']}")
    print(f"Plans imported:            {summary['plans']}")
    print(f"IndexedDB sources:         {summary['indexeddb_sources']}")
    print(f"IndexedDB accounts:        {summary['indexeddb_accounts']}")
    print(f"IndexedDB conversations:   {summary['indexeddb_conversations']}")
    print(f"IndexedDB drafts:          {summary['indexeddb_drafts']}")


if __name__ == "__main__":
    main()
