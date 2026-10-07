"""Release-owned member bridge for qualified projects and agent log commands."""
from __future__ import annotations

import argparse
import json
import re
import sys
from typing import TextIO

from ..clients.config import ClientProfile, _validate_base_url, load_member_profile
from ..clients.errors import ClientConfigError, CortexTransportError
from ..clients.key_store import KeyStoreError
from ..clients.transport import HttpResponse, http_request


class FacadeUnavailable(ClientConfigError):
    """The requested caller contract has not been mapped in this release."""


class _LogUsage(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **dict(kwargs, allow_abbrev=False))

    def error(self, message):
        # ArgumentParser normally echoes caller argv, which may contain a URL.
        raise FacadeUnavailable("facade request unavailable in this release")


class _SingleValue(argparse.Action):
    def __call__(self, parser, namespace, value, option_string=None):
        if getattr(namespace, self.dest, None) is not None:
            raise FacadeUnavailable("facade request unavailable in this release")
        setattr(namespace, self.dest, value)


def request_projects(
    profile: ClientProfile, *, method: str = "GET", path: str = "/projects",
    payload=None, agent_name: str = "",
) -> HttpResponse:
    if method != "GET" or path != "/projects" or payload is not None:
        raise FacadeUnavailable("facade request unavailable in this release")
    reader = profile.member_reader
    if (reader is None or profile.default_scope != reader.project
            or any(scope != reader.project for scope in profile.default_read_scopes)
            or (agent_name and agent_name != getattr(profile, "principal_label", None))):
        raise ClientConfigError("request must use its selected project member")
    url = _validate_base_url(profile.base_url) + path
    try:
        headers = reader.headers()
    except KeyStoreError:
        raise ClientConfigError("member credential unavailable; ask the project lead for enrollment or unlock") from None
    return http_request("GET", url, headers=headers)


def format_projects(data) -> str:
    if not isinstance(data, dict) or not isinstance(data.get("projects"), list):
        raise ClientConfigError("invalid project response")
    projects = data["projects"]
    for row in projects:
        if not isinstance(row, dict):
            raise ClientConfigError("invalid project row")
        for name in ("project_key", "project_id", "display_name", "status", "repo_root", "created_at", "updated_at"):
            if not isinstance(row.get(name), str) or not row[name]:
                raise ClientConfigError("incomplete project row")
        for name in ("default_agent", "parent_project_key"):
            if name not in row or (row[name] is not None and not isinstance(row[name], str)):
                raise ClientConfigError("incomplete project row")
        for name in ("agent_count", "profile_count"):
            if type(row.get(name)) is not int or row[name] < 0:
                raise ClientConfigError("invalid project counts")
        roots = row.get("roots")
        if not isinstance(roots, list) or not roots:
            raise ClientConfigError("invalid project roots")
        for root in roots:
            if (not isinstance(root, dict) or not isinstance(root.get("path"), str)
                    or not root["path"] or not isinstance(root.get("kind"), str) or not root["kind"]):
                raise ClientConfigError("invalid project roots")
        primary = [root for root in roots if root["kind"] == "primary"]
        if len(primary) != 1 or primary[0]["path"] != row["repo_root"]:
            raise ClientConfigError("inconsistent project roots")
    if not projects:
        return "(no registered projects)\n"
    lines = ["", "## Cortex Projects", "",
        f'{"Key":<12} | {"Name":<28} | {"Parent":<12} | {"Status":<8} | '
        f'{"Agents":<6} | {"Profiles":<8} | Root',
        "-" * 12 + "-+-" + "-" * 28 + "-+-" + "-" * 12 + "-+-" + "-" * 8 + "-+-"
        + "-" * 6 + "-+-" + "-" * 8 + "-+-" + "-" * 40]
    for row in projects:
        lines.append(f'{row["project_key"]:<12} | {row["display_name"]:<28} | '
                     f'{(row["parent_project_key"] or "-"):<12} | {row["status"]:<8} | '
                     f'{str(row["agent_count"]):<6} | {str(row["profile_count"]):<8} | {row["repo_root"]}')
    return "\n".join(lines) + "\n\n"


def main(argv=None, *, stdin: TextIO | None = None, stdout: TextIO | None = None,
         stderr: TextIO | None = None) -> int:
    stdin, stdout, stderr = stdin or sys.stdin, stdout or sys.stdout, stderr or sys.stderr
    parser = _Parser(description=__doc__)
    parser.add_argument("--config", action=_SingleValue,
                        help="non-secret v2 member connection profile")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("projects", help="show registered projects using the selected member")
    log = commands.add_parser('log', help='record and confirm the selected agent event')
    def log_usage(*unused, **kwargs):
        from .agent_log import USAGE
        stdout.write(USAGE)
        raise _LogUsage
    log.print_help = log_usage
    log.error = log_usage
    log.add_argument('--goal', '--goal-parent', action=_SingleValue)
    confirmation = log.add_mutually_exclusive_group()
    confirmation.add_argument('--confirm', dest='confirm', action='store_true', default=None)
    confirmation.add_argument('--no-confirm', dest='confirm', action='store_false')
    log.add_argument('arguments', nargs=argparse.REMAINDER)
    api = commands.add_parser("api", help="raw qualified facade response")
    api.add_argument("method")
    api.add_argument("path")
    api.add_argument("--agent-name", action=_SingleValue)
    try:
        args = parser.parse_args(argv)
        if args.command == 'log':
            if args.arguments[:1] == ['--']:
                args.arguments = args.arguments[1:]
            if len(args.arguments) < 3:
                log_usage()
            args.agent, args.event_type, args.summary, *args.files = args.arguments
            from .agent_log import prepare, run
            prepared = prepare(args)
            profile = load_member_profile(args.config)
            stdout.write(run(profile, args, prepared))
            return 0
        # Validate caller data before profile construction or credential access.
        if args.command == "api":
            if args.method != "GET" or args.path != "/projects" or stdin.read(1):
                raise FacadeUnavailable("facade request unavailable in this release")
        profile = load_member_profile(args.config)
        response = request_projects(profile, agent_name=getattr(args, "agent_name", ""))
        if response.status != 200:
            # Never echo arbitrary server bodies or private transport diagnostics.
            code = ""
            try:
                body = json.loads(response.body)
                error = body.get("error") if isinstance(body, dict) else None
                value = error.get("code") if isinstance(error, dict) else None
                if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,95}", value):
                    code = ": " + value
            except (ValueError, UnicodeError):
                pass
            print(f"API error {response.status}{code}", file=stderr)
            return 22
        if args.command == "projects":
            output = format_projects(json.loads(response.body))
        else:
            output = response.body.decode("utf-8")
        stdout.write(output)
        return 0
    except _LogUsage:
        return 1
    except FacadeUnavailable:
        print("ERROR: facade request unavailable in this release", file=stderr)
        return 2
    except (ClientConfigError, KeyStoreError, ValueError, UnicodeError):
        print("ERROR: member project request refused; check its v2 profile, enrollment and response contract", file=stderr)
        return 2
    except CortexTransportError:
        print("ERROR: selected Cortex connection failed", file=stderr)
        return 7


if __name__ == "__main__":
    raise SystemExit(main())
