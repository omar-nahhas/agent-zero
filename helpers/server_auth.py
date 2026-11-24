"""Header-based authentication and project routing for the MCP and A2A servers.

Both servers have always taken the API token and the project from the URL
(``/t-<token>/`` and ``/p-<project>/``). URLs end up in proxy, browser and
access logs, so clients may instead send:

* ``Authorization: Bearer <token>`` - the same token as in the URL;
* ``X-A0-Project: <project>`` - the same project name as in the URL.

The URL form keeps working unchanged. Never log the token or the header value.

With the ``mcp_a2a_project_allowlist`` setting on (off by default), only projects
whose "Reachable through MCP/A2A" setting is on (``remote_access`` in the
project's ``.a0proj/project.json``, off by default) can be reached: every MCP and
A2A request must name such a project, or it is rejected before any chat is
created (see ``require_remote_access``). With it off, any project or none is
reachable, as before.
"""

import hmac

from helpers import projects, settings

PROJECT_HEADER = "X-A0-Project"


class RequestRejected(Exception):
    """The request carries an invalid or conflicting project."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def token_matches(candidate: str | None, expected: str | None) -> bool:
    """Constant-time token comparison; an empty token never matches."""
    if not candidate or not expected:
        return False
    return hmac.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))


def get_header(scope, name: str) -> str | None:
    """Return the first value of header ``name`` from an ASGI scope, or None."""
    wanted = name.lower().encode("latin-1")
    for key, value in scope.get("headers") or []:
        if key.lower() == wanted:
            return value.decode("latin-1")
    return None


def get_bearer_token(scope) -> str | None:
    """Return the token of an ``Authorization: Bearer <token>`` header, or None."""
    value = get_header(scope, "authorization")
    if not value:
        return None
    scheme, _, token = value.strip().partition(" ")
    if scheme.lower() != "bearer":
        return None
    return token.strip() or None


def resolve_project(url_project: str | None, scope) -> str | None:
    """Return the project from the URL segment or the X-A0-Project header.

    Raises RequestRejected (400) when either is not a valid project name, or
    when both are given and differ.
    """
    if url_project:
        try:
            url_project = projects.validate_project_name(url_project)
        except ValueError:
            raise RequestRejected(400, "Invalid project in URL")

    header_project = None
    header_value = get_header(scope, PROJECT_HEADER)
    if header_value is not None and header_value.strip():
        try:
            header_project = projects.validate_project_name(header_value)
        except ValueError:
            raise RequestRejected(400, f"Invalid project in {PROJECT_HEADER} header")

    if url_project and header_project and url_project != header_project:
        raise RequestRejected(
            400, f"Project in URL and {PROJECT_HEADER} header differ"
        )
    return url_project or header_project


def project_allowlist_enabled() -> bool:
    """Whether the mcp_a2a_project_allowlist setting is on."""
    return settings.get_settings().get("mcp_a2a_project_allowlist") is True


def require_remote_access(project_name: str | None) -> str | None:
    """Return ``project_name`` if the MCP and A2A servers may reach it.

    With the ``mcp_a2a_project_allowlist`` setting off, every project (or none)
    is reachable and ``project_name`` is returned unchanged. With it on, raises
    RequestRejected:

    * 400 when no project is given (there is no fallback to a default project);
    * 400 when there is no such project;
    * 403 when the project's "Reachable through MCP/A2A" setting is off.
    """
    if not project_allowlist_enabled():
        return project_name
    if not project_name:
        raise RequestRejected(
            400,
            f"A project is required: send the {PROJECT_HEADER} header or a /p-<project>/ URL segment",
        )
    allowed = projects.get_remote_access(project_name)
    if allowed is None:
        raise RequestRejected(400, f"Unknown project '{project_name}'")
    if not allowed:
        raise RequestRejected(
            403, f"Project '{project_name}' is not reachable through MCP/A2A"
        )
    return project_name
