"""Opt-in per-project allowlist for the MCP and A2A servers.

With the global ``mcp_a2a_project_allowlist`` setting off (the default) nothing
changes: any project, or none, is reachable. With it on, a project is reachable
through MCP/A2A only when its "Reachable through MCP/A2A" setting
(``remote_access`` in ``.a0proj/project.json``, off by default) is on. Every
request must then name such a project (URL ``/p-<project>/`` or the
``X-A0-Project`` header) and is otherwise rejected before any chat is created:
no project or an unknown project -> 400, a project with the setting off -> 403.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import os
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TOKEN = "Tok3n-For-Tests1"
BEARER = (("authorization", f"Bearer {TOKEN}"),)

# open = reachable, closed = exists with the setting off; anything else is unknown
REMOTE_ACCESS = {"open": True, "open2": True, "closed": False}


# ---------------------------------------------------------------------------
# helpers/projects.py: the setting itself
# ---------------------------------------------------------------------------


def _load_projects(monkeypatch, tmp_path):
    """Import helpers/projects.py with its file helpers pointed at tmp_path."""

    def get_abs_path(*parts):
        return os.path.join(str(tmp_path), *parts)

    def read_file(path, encoding="utf-8"):
        with open(get_abs_path(path), encoding=encoding) as f:
            return f.read()

    def write_file(path, content, encoding="utf-8"):
        path = get_abs_path(path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding=encoding) as f:
            f.write(content)

    files_stub = types.ModuleType("helpers.files")
    files_stub.get_abs_path = get_abs_path
    files_stub.read_file = read_file
    files_stub.write_file = write_file

    extension_stub = types.ModuleType("helpers.extension")
    extension_stub.extensible = lambda fn: fn

    print_style_stub = types.ModuleType("helpers.print_style")
    print_style_stub.PrintStyle = type("PrintStyle", (), {"error": staticmethod(lambda *a, **k: None)})

    file_tree_stub = types.ModuleType("helpers.file_tree")
    file_tree_stub.OUTPUT_MODE_STRING = "string"

    package = types.ModuleType("helpers")
    package.__path__ = [str(ROOT / "helpers")]
    monkeypatch.setitem(sys.modules, "helpers", package)
    for name, module in {
        "helpers.files": files_stub,
        "helpers.extension": extension_stub,
        "helpers.print_style": print_style_stub,
        "helpers.persist_chat": types.ModuleType("helpers.persist_chat"),
        "helpers.file_tree": file_tree_stub,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.delitem(sys.modules, "helpers.dirty_json", raising=False)

    spec = importlib.util.spec_from_file_location("helpers.projects", ROOT / "helpers" / "projects.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "helpers.projects", module)
    spec.loader.exec_module(module)
    return module


def _write_header(tmp_path, name, header):
    meta = tmp_path / "usr" / "projects" / name / ".a0proj"
    meta.mkdir(parents=True, exist_ok=True)
    (meta / "project.json").write_text(json.dumps(header), encoding="utf-8")
    return meta / "project.json"


def test_remote_access_defaults_off_and_saves(monkeypatch, tmp_path):
    projects = _load_projects(monkeypatch, tmp_path)
    header = _write_header(tmp_path, "demo", {"title": "Demo"})

    data = projects.load_basic_project_data("demo")
    assert data["remote_access"] is False
    assert projects.get_remote_access("demo") is False

    projects.save_project_header("demo", data)
    assert json.loads(header.read_text())["remote_access"] is False

    data["remote_access"] = True
    projects.save_project_header("demo", data)
    assert json.loads(header.read_text())["remote_access"] is True
    assert projects.get_remote_access("demo") is True


@pytest.mark.parametrize(
    "value,expected",
    [(True, True), (False, False), ("true", True), ("false", False), (None, False), (1, False), ("", False)],
)
def test_remote_access_is_on_only_when_explicitly_true(monkeypatch, tmp_path, value, expected):
    projects = _load_projects(monkeypatch, tmp_path)
    _write_header(tmp_path, "demo", {"title": "Demo", "remote_access": value})
    assert projects.get_remote_access("demo") is expected


def test_remote_access_unknown_or_invalid_project(monkeypatch, tmp_path):
    projects = _load_projects(monkeypatch, tmp_path)
    (tmp_path / "usr" / "projects" / "bare").mkdir(parents=True)  # a folder without .a0proj
    for name in ("missing", "bare", "", None, "..", "a/b"):
        assert projects.get_remote_access(name) is None


def test_remote_access_unreadable_header_is_off(monkeypatch, tmp_path):
    projects = _load_projects(monkeypatch, tmp_path)
    header = _write_header(tmp_path, "demo", {})
    header.write_text("not json at all [", encoding="utf-8")
    assert projects.get_remote_access("demo") is False


# ---------------------------------------------------------------------------
# The servers, with stubbed Agent Zero modules
# ---------------------------------------------------------------------------


def _stub(monkeypatch, name, module):
    """Install a stub submodule of helpers, also as the package attribute that
    "from helpers import x" reads first."""
    monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(sys.modules["helpers"], name.split(".", 1)[1], module, raising=False)


def _install_stubs(monkeypatch, allowlist):
    importlib.import_module("helpers")

    settings_stub = types.ModuleType("helpers.settings")
    settings_stub.get_settings = lambda: {
        "mcp_server_enabled": True,
        "a2a_server_enabled": True,
        "mcp_server_token": TOKEN,
        "mcp_a2a_project_allowlist": allowlist,
    }
    _stub(monkeypatch, "helpers.settings", settings_stub)

    projects_stub = types.ModuleType("helpers.projects")
    projects_stub.CONTEXT_DATA_KEY_PROJECT = "project"
    projects_stub.activated = []

    def validate_project_name(name):
        candidate = str(name or "").strip()
        if not candidate or candidate in {".", ".."} or os.path.basename(candidate) != candidate:
            raise ValueError("Invalid project name")
        return candidate

    projects_stub.validate_project_name = validate_project_name
    projects_stub.get_remote_access = lambda name: REMOTE_ACCESS.get(name)
    projects_stub.activate_project = lambda context_id, name, **kw: projects_stub.activated.append(
        (context_id, name)
    )
    _stub(monkeypatch, "helpers.projects", projects_stub)

    print_style_stub = types.ModuleType("helpers.print_style")

    class _PrintStyle:
        def __init__(self, *args, **kwargs):
            pass

        def print(self, *args, **kwargs):
            pass

        @staticmethod
        def error(*args, **kwargs):
            pass

    print_style_stub.PrintStyle = _PrintStyle
    _stub(monkeypatch, "helpers.print_style", print_style_stub)

    class _Context:
        contexts: dict = {}
        created: list = []

        def __init__(self, config=None, type=None):
            self.id = f"ctx-{len(_Context.created) + 1}"
            self.data = {}
            self.log = types.SimpleNamespace(log=lambda **kw: None)
            _Context.contexts[self.id] = self
            _Context.created.append(self.id)

        @classmethod
        def get(cls, context_id):
            return cls.contexts.get(context_id)

        @classmethod
        def remove(cls, context_id):
            cls.contexts.pop(context_id, None)

        def get_data(self, key):
            return self.data.get(key)

        def reset(self):
            pass

        def communicate(self, message):
            async def result():
                return "done"
            return types.SimpleNamespace(result=result)

    _Context.contexts = {}
    _Context.created = []

    agent_stub = types.ModuleType("agent")
    agent_stub.AgentContext = _Context
    agent_stub.UserMessage = lambda **kwargs: types.SimpleNamespace(**kwargs)
    agent_stub.AgentContextType = types.SimpleNamespace(BACKGROUND="background")
    monkeypatch.setitem(sys.modules, "agent", agent_stub)

    initialize_stub = types.ModuleType("initialize")
    initialize_stub.calls = []
    initialize_stub.initialize_agent = lambda: initialize_stub.calls.append(1) or {}
    monkeypatch.setitem(sys.modules, "initialize", initialize_stub)

    persist_chat_stub = types.ModuleType("helpers.persist_chat")
    persist_chat_stub.remove_chat = lambda *args, **kwargs: None
    _stub(monkeypatch, "helpers.persist_chat", persist_chat_stub)

    # Fresh server modules bound to these stubs (both are imported, so that the
    # package attributes dropped here exist again when monkeypatch restores them)
    for name in ("helpers.server_auth", "helpers.mcp_server", "helpers.fasta2a_server"):
        monkeypatch.delitem(sys.modules, name, raising=False)
        # drop the package attribute too, and have monkeypatch restore it afterwards
        package, attr = sys.modules["helpers"], name.split(".", 1)[1]
        monkeypatch.setattr(package, attr, None, raising=False)
        delattr(package, attr)
    importlib.import_module("helpers.mcp_server")
    importlib.import_module("helpers.fasta2a_server")
    return types.SimpleNamespace(projects=projects_stub, context=_Context, initialize=initialize_stub)


def _mcp_env(monkeypatch, allowlist):
    env = _install_stubs(monkeypatch, allowlist)
    env.module = importlib.import_module("helpers.mcp_server")
    env.module.DynamicMcpProxy._instance = None
    return env


def _a2a_env(monkeypatch, allowlist):
    env = _install_stubs(monkeypatch, allowlist)
    env.module = importlib.import_module("helpers.fasta2a_server")
    return env


@pytest.fixture
def mcp(monkeypatch):
    """MCP server with the project allowlist on."""
    return _mcp_env(monkeypatch, True)


@pytest.fixture
def mcp_off(monkeypatch):
    """MCP server with the project allowlist off (the default)."""
    return _mcp_env(monkeypatch, False)


@pytest.fixture
def a2a(monkeypatch):
    return _a2a_env(monkeypatch, True)


@pytest.fixture
def a2a_off(monkeypatch):
    return _a2a_env(monkeypatch, False)


def _nothing_created(env):
    assert env.context.created == []
    assert env.initialize.calls == []
    assert env.projects.activated == []


def _scope(method, path, headers=(), query=b"", root_path=""):
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 1234),
        "path": path,
        "raw_path": path.encode(),
        "root_path": root_path,
        "query_string": query,
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers],
    }


async def _request(app, method, path, headers=(), body=b"", query=b""):
    sent = []
    done = False

    async def receive():
        nonlocal done
        if not done:
            done = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.Event().wait()

    async def send(message):
        sent.append(message)

    await app(_scope(method, path, headers, query), receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    payload = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], payload


def _mcp_app(module):
    from starlette.applications import Starlette
    from starlette.routing import Mount

    return Starlette(routes=[Mount("/mcp", app=module.DynamicMcpProxy.get_instance())])


def _recording_proxy(module):
    calls = []

    async def recorder(scope, receive, send):
        calls.append((scope["path"], module._mcp_project_name.get()))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    proxy = module.DynamicMcpProxy.get_instance()
    proxy.sse_app = recorder
    proxy.http_app = recorder
    return proxy, calls


def _project(name):
    return (("x-a0-project", name),)


MCP_ENDPOINTS = ("sse", "messages/", "http/")


def _mcp_forms(endpoint, project):
    """(path, headers) for one project: header auth, URL token, and all-URL."""
    if project is None:
        return [(f"/mcp/{endpoint}", BEARER), (f"/mcp/t-{TOKEN}/{endpoint}", ())]
    return [
        (f"/mcp/{endpoint}", BEARER + _project(project)),
        (f"/mcp/p-{project}/{endpoint}", BEARER),
        (f"/mcp/t-{TOKEN}/{endpoint}", _project(project)),
        (f"/mcp/t-{TOKEN}/p-{project}/{endpoint}", ()),
    ]


# ---------------------------------------------------------------------------
# MCP
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("endpoint", MCP_ENDPOINTS)
def test_mcp_allowlisted_project_is_accepted(mcp, endpoint):
    proxy, calls = _recording_proxy(mcp.module)
    app = _mcp_app(mcp.module)
    for path, headers in _mcp_forms(endpoint, "open"):
        status, _ = asyncio.run(_request(app, "POST", path, headers))
        assert status == 200, (path, headers)
    assert [project for _, project in calls] == ["open"] * 4


@pytest.mark.parametrize("endpoint", MCP_ENDPOINTS)
@pytest.mark.parametrize(
    "project,status,detail",
    [
        (None, 400, b"A project is required"),
        ("nosuch", 400, b"Unknown project 'nosuch'"),
        ("closed", 403, b"Project 'closed' is not reachable through MCP/A2A"),
    ],
    ids=["missing", "unknown", "not-allowlisted"],
)
def test_mcp_rejects_missing_unknown_and_not_allowlisted(mcp, endpoint, project, status, detail):
    proxy, calls = _recording_proxy(mcp.module)
    app = _mcp_app(mcp.module)
    for path, headers in _mcp_forms(endpoint, project):
        code, body = asyncio.run(_request(app, "POST", path, headers))
        assert code == status, (path, headers)
        assert detail in body
    assert calls == []
    _nothing_created(mcp)


def test_mcp_real_transports_reject_before_any_session(mcp):
    """With the real SSE and streamable-HTTP apps: rejected, no stream opened."""
    init = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "test", "version": "1"}},
    }).encode()
    common = (("content-type", "application/json"), ("accept", "application/json, text/event-stream"))

    async def run():
        app = _mcp_app(mcp.module)
        results = []
        for project, expected in ((None, 400), ("nosuch", 400), ("closed", 403)):
            headers = BEARER + (_project(project) if project else ())
            results.append((await _request(app, "GET", "/mcp/sse", headers + (("accept", "text/event-stream"),)))[0] == expected)
            results.append((await _request(app, "POST", "/mcp/http/", headers + common, init))[0] == expected)
        return results

    assert all(asyncio.run(run()))
    assert mcp.module.DynamicMcpProxy.get_instance().http_session_task_group is None
    _nothing_created(mcp)


def test_mcp_streamable_http_allowlisted_project_initializes(mcp):
    init = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "test", "version": "1"}},
    }).encode()
    common = (("content-type", "application/json"), ("accept", "application/json, text/event-stream"))

    async def run():
        import anyio

        app = _mcp_app(mcp.module)
        proxy = mcp.module.DynamicMcpProxy.get_instance()
        task_group = anyio.create_task_group()
        await task_group.__aenter__()
        proxy.http_session_task_group = task_group
        proxy.http_session_manager._task_group = task_group
        try:
            return await _request(app, "POST", "/mcp/http/", BEARER + _project("open") + common, init)
        finally:
            task_group.cancel_scope.cancel()
            await task_group.__aexit__(None, None, None)

    status, body = asyncio.run(run())
    assert status == 200 and b'"serverInfo"' in body


def _tool(module, name):
    fn = getattr(module, name)
    return getattr(fn, "fn", fn)


async def _fake_run_chat(context, message, attachments=None):
    return "ok"


@pytest.mark.parametrize("project", [None, "nosuch", "closed"])
def test_mcp_send_message_rejects_without_creating_a_chat(mcp, project):
    mcp.module._run_chat = _fake_run_chat

    async def run():
        mcp.module._mcp_project_name.set(project)
        return await _tool(mcp.module, "send_message")(message="hi", persistent_chat=True)

    result = asyncio.run(run())
    assert result.status == "error"
    _nothing_created(mcp)


def test_mcp_send_message_allowlisted_project_creates_chat_in_it(mcp):
    mcp.module._run_chat = _fake_run_chat

    async def run():
        mcp.module._mcp_project_name.set("open")
        return await _tool(mcp.module, "send_message")(message="hi", persistent_chat=True)

    result = asyncio.run(run())
    assert result.status == "success"
    assert mcp.projects.activated == [(result.chat_id, "open")]


@pytest.mark.parametrize("chat_project", ["open2", None])
def test_mcp_chat_id_from_another_project_is_rejected(mcp, chat_project):
    mcp.module._run_chat = _fake_run_chat
    other = mcp.context()
    other.data["project"] = chat_project

    async def run():
        mcp.module._mcp_project_name.set("open")
        sent = await _tool(mcp.module, "send_message")(message="hi", chat_id=other.id)
        finished = await _tool(mcp.module, "finish_chat")(chat_id=other.id)
        return sent, finished

    sent, finished = asyncio.run(run())
    assert sent.status == "error" and "belongs to project" in sent.error
    assert finished.status == "error" and "belongs to project" in finished.error
    assert mcp.context.get(other.id) is other  # not finished
    assert mcp.projects.activated == []


def test_mcp_chat_id_of_the_same_project_continues_and_finishes(mcp):
    mcp.module._run_chat = _fake_run_chat
    own = mcp.context()
    own.data["project"] = "open"

    async def run():
        mcp.module._mcp_project_name.set("open")
        sent = await _tool(mcp.module, "send_message")(message="hi", chat_id=own.id)
        finished = await _tool(mcp.module, "finish_chat")(chat_id=own.id)
        return sent, finished

    sent, finished = asyncio.run(run())
    assert sent.status == "success" and sent.chat_id == own.id
    assert finished.status == "success"
    assert mcp.context.get(own.id) is None


def test_mcp_finish_chat_rejects_not_allowlisted_project(mcp):
    chat = mcp.context()
    chat.data["project"] = "closed"

    async def run():
        mcp.module._mcp_project_name.set("closed")
        return await _tool(mcp.module, "finish_chat")(chat_id=chat.id)

    result = asyncio.run(run())
    assert result.status == "error" and "not reachable" in result.error
    assert mcp.context.get(chat.id) is chat


# ---------------------------------------------------------------------------
# A2A
# ---------------------------------------------------------------------------

A2A_BODY = json.dumps({
    "jsonrpc": "2.0", "id": "1", "method": "message/send",
    "params": {"message": {"role": "user", "kind": "message", "message_id": "m1",
                           "parts": [{"kind": "text", "text": "hi"}]}},
}).encode()


def _a2a_proxy(module):
    calls = []

    async def app(scope, receive, send):
        message = await receive()
        calls.append(json.loads(message.get("body", b"{}")))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    proxy = object.__new__(module.DynamicA2AProxy)
    proxy.app = app
    proxy.token = TOKEN
    proxy._lock = __import__("threading").Lock()
    proxy._startup_done = True
    proxy._reconfigure_needed = False
    return proxy, calls


def _a2a_forms(project):
    if project is None:
        return [("/a2a/", BEARER), (f"/a2a/t-{TOKEN}/", ())]
    return [
        ("/a2a/", BEARER + _project(project)),
        (f"/a2a/t-{TOKEN}/", _project(project)),
        (f"/a2a/t-{TOKEN}/p-{project}/", ()),
    ]


needs_fasta2a = pytest.mark.skipif(
    importlib.util.find_spec("fasta2a") is None, reason="fasta2a not installed"
)


@needs_fasta2a
def test_a2a_allowlisted_project_is_accepted(a2a):
    proxy, calls = _a2a_proxy(a2a.module)
    for path, headers in _a2a_forms("open"):
        assert asyncio.run(_request(proxy, "POST", path, headers, A2A_BODY))[0] == 200
    assert [c["params"]["message"]["metadata"]["project"] for c in calls] == ["open"] * 3


@needs_fasta2a
@pytest.mark.parametrize(
    "project,status,detail",
    [
        (None, 400, b"A project is required"),
        ("nosuch", 400, b"Unknown project 'nosuch'"),
        ("closed", 403, b"Project 'closed' is not reachable through MCP/A2A"),
    ],
    ids=["missing", "unknown", "not-allowlisted"],
)
def test_a2a_rejects_missing_unknown_and_not_allowlisted(a2a, project, status, detail):
    proxy, calls = _a2a_proxy(a2a.module)
    for path, headers in _a2a_forms(project):
        code, body = asyncio.run(_request(proxy, "POST", path, headers, A2A_BODY))
        assert code == status, (path, headers)
        assert detail in body
    # the agent card is behind the same check
    code, _ = asyncio.run(_request(proxy, "GET", "/a2a/.well-known/agent.json",
                                   BEARER + (_project(project) if project else ())))
    assert code == status
    assert calls == []
    _nothing_created(a2a)


class _Storage:
    def __init__(self):
        self.updates = []

    async def update_task(self, **kwargs):
        self.updates.append(kwargs)


def _run_worker(module, metadata):
    storage = _Storage()
    worker = object.__new__(module.AgentZeroWorker)
    worker.storage = storage
    message = {"role": "user", "parts": [{"kind": "text", "text": "hi"}]}
    if metadata is not None:
        message["metadata"] = metadata
    asyncio.run(worker.run_task({"id": "task-1", "message": message}))
    return storage.updates


@needs_fasta2a
@pytest.mark.parametrize(
    "metadata", [None, {}, {"project": "nosuch"}, {"project": "closed"}, {"project": ".."}]
)
def test_a2a_worker_rejects_without_creating_a_context(a2a, metadata):
    updates = _run_worker(a2a.module, metadata)
    assert updates == [{"task_id": "task-1", "state": "failed"}]
    _nothing_created(a2a)


@needs_fasta2a
def test_a2a_worker_runs_an_allowlisted_project(a2a):
    updates = _run_worker(a2a.module, {"project": "open"})
    assert updates[-1]["state"] == "completed"
    assert [name for _, name in a2a.projects.activated] == ["open"]


# ---------------------------------------------------------------------------
# Allowlist off (the default): unchanged behaviour
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("endpoint", MCP_ENDPOINTS)
def test_off_mcp_accepts_no_project_any_project_and_unflagged(mcp_off, endpoint):
    proxy, calls = _recording_proxy(mcp_off.module)
    app = _mcp_app(mcp_off.module)
    forms = _mcp_forms(endpoint, None) + _mcp_forms(endpoint, "nosuch") + _mcp_forms(endpoint, "closed")
    for path, headers in forms:
        status, _ = asyncio.run(_request(app, "POST", path, headers))
        assert status == 200, (path, headers)
    assert [p for _, p in calls] == [None] * 2 + ["nosuch"] * 4 + ["closed"] * 4


@pytest.mark.parametrize("project", [None, "closed"])
def test_off_mcp_send_message_runs_without_or_with_unflagged_project(mcp_off, project):
    mcp_off.module._run_chat = _fake_run_chat

    async def run():
        mcp_off.module._mcp_project_name.set(project)
        return await _tool(mcp_off.module, "send_message")(message="hi", persistent_chat=True)

    result = asyncio.run(run())
    assert result.status == "success"
    assert mcp_off.projects.activated == ([(result.chat_id, project)] if project else [])


def test_off_mcp_chat_id_check_is_unchanged(mcp_off):
    """Only a chat of another named project is refused, as before."""
    mcp_off.module._run_chat = _fake_run_chat
    other = mcp_off.context()
    other.data["project"] = "open2"
    plain = mcp_off.context()

    async def run():
        mcp_off.module._mcp_project_name.set("open")
        refused = await _tool(mcp_off.module, "send_message")(message="hi", chat_id=other.id)
        continued = await _tool(mcp_off.module, "send_message")(message="hi", chat_id=plain.id)
        mcp_off.module._mcp_project_name.set(None)
        no_project = await _tool(mcp_off.module, "send_message")(message="hi", chat_id=other.id)
        finished = await _tool(mcp_off.module, "finish_chat")(chat_id=other.id)
        return refused, continued, no_project, finished

    refused, continued, no_project, finished = asyncio.run(run())
    assert refused.status == "error" and "belongs to project 'open2'" in refused.error
    assert continued.status == "success"
    assert no_project.status == "success"
    assert finished.status == "success"


@needs_fasta2a
def test_off_a2a_accepts_no_project_any_project_and_unflagged(a2a_off):
    proxy, calls = _a2a_proxy(a2a_off.module)
    forms = _a2a_forms(None) + _a2a_forms("nosuch") + _a2a_forms("closed")
    for path, headers in forms:
        assert asyncio.run(_request(proxy, "POST", path, headers, A2A_BODY))[0] == 200, (path, headers)
    projects = [(c["params"]["message"].get("metadata") or {}).get("project") for c in calls]
    assert projects == [None] * 2 + ["nosuch"] * 3 + ["closed"] * 3


@needs_fasta2a
@pytest.mark.parametrize("metadata,activated", [(None, []), ({"project": "closed"}, ["closed"])])
def test_off_a2a_worker_runs_without_or_with_unflagged_project(a2a_off, metadata, activated):
    updates = _run_worker(a2a_off.module, metadata)
    assert updates[-1]["state"] == "completed"
    assert [name for _, name in a2a_off.projects.activated] == activated


async def _chunked_request(app, path, headers, chunks):
    """POST ``chunks`` as separate http.request events; return (status, body)."""
    pending = [
        {"type": "http.request", "body": chunk, "more_body": i < len(chunks) - 1}
        for i, chunk in enumerate(chunks)
    ]
    sent = []

    async def receive():
        if pending:
            return pending.pop(0)
        await asyncio.Event().wait()

    async def send(message):
        sent.append(message)

    await app(_scope("POST", path, headers), receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    payload = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], payload


def _a2a_reading_proxy(module):
    """A2A proxy whose app reads every http.request event until more_body is false."""
    events = []

    async def app(scope, receive, send):
        while True:
            message = await receive()
            events.append(message)
            if message["type"] != "http.request" or not message.get("more_body", False):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    proxy, _ = _a2a_proxy(module)
    proxy.app = app
    return proxy, events


A2A_CHUNKS = [A2A_BODY[:10], A2A_BODY[10:40], A2A_BODY[40:]]


@needs_fasta2a
def test_a2a_allowlisted_project_chunked_body_is_rewritten_once(a2a):
    proxy, events = _a2a_reading_proxy(a2a.module)
    for path, headers in _a2a_forms("open"):
        events.clear()
        status, _ = asyncio.run(_chunked_request(proxy, path, headers, A2A_CHUNKS))
        assert status == 200, (path, headers)
        assert len(events) == 1 and events[0]["more_body"] is False
        expected = json.loads(A2A_BODY)
        expected["params"]["message"]["metadata"] = {"project": "open"}
        assert json.loads(events[0]["body"]) == expected


@needs_fasta2a
@pytest.mark.parametrize("project,status", [(None, 400), ("nosuch", 400), ("closed", 403)])
def test_a2a_chunked_body_rejected_before_the_app_reads_it(a2a, project, status):
    proxy, events = _a2a_reading_proxy(a2a.module)
    for path, headers in _a2a_forms(project):
        code, _ = asyncio.run(_chunked_request(proxy, path, headers, A2A_CHUNKS))
        assert code == status, (path, headers)
    assert events == []
    _nothing_created(a2a)


@needs_fasta2a
def test_off_a2a_chunked_body_with_unflagged_project_is_rewritten_once(a2a_off):
    proxy, events = _a2a_reading_proxy(a2a_off.module)
    status, _ = asyncio.run(_chunked_request(proxy, f"/a2a/t-{TOKEN}/p-closed/", (), A2A_CHUNKS))
    assert status == 200
    assert len(events) == 1
    assert json.loads(events[0]["body"])["params"]["message"]["metadata"] == {"project": "closed"}
