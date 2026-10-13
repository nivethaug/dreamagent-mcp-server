#!/usr/bin/env python3
"""
DreamAgent MCP Server — standalone.

Lets ChatGPT (or any MCP client) operate the user's DreamAgent account:
create projects (websites, Telegram/Discord bots, AI agents, automations), check their
status, and edit them conversationally through DreamAgent's AI agent.

Uses ONLY existing DreamAgent REST endpoints — the backend is never
modified, and all server-side ownership checks apply unchanged.

Run:
  stdio (local testing):        python server.py
  HTTP (remote MCP / ChatGPT): python server.py --http --port 8800

Config (env or .env):
  DREAMAGENT_API_URL    default https://api.dreamagent.cloud
  DREAMAGENT_EMAIL      the user's DreamAgent account email
  DREAMAGENT_PASSWORD   the user's DreamAgent account password
"""

import argparse
import base64
import logging
import os
import re
import sys

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers
from mcp.types import ImageContent, TextContent, ToolAnnotations

from auth import AuthManager, AuthError
from dreamagent_client import DreamAgentClient, DreamAgentAPIError, PROJECT_TYPES
from dreamagent_client import set_request_token, get_request_token
import oauth as oauth_as

# ---------------------------------------------------------------------------
# MCP tool annotations (readOnlyHint / openWorldHint / destructiveHint).
# Shared objects so all 12 tools expose consistent, explicit metadata.
# ---------------------------------------------------------------------------
_READ_ONLY = ToolAnnotations(
    readOnlyHint=True, openWorldHint=False, destructiveHint=False)
_WRITES_PROJECT = ToolAnnotations(
    readOnlyHint=False, openWorldHint=True, destructiveHint=False)
_WRITES_LOCAL = ToolAnnotations(
    readOnlyHint=False, openWorldHint=False, destructiveHint=False)
_DESTRUCTIVE = ToolAnnotations(
    readOnlyHint=False, openWorldHint=False, destructiveHint=True)

register_oauth = oauth_as.register_oauth_routes

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("dreamagent.mcp")

mcp = FastMCP(
    "DreamAgent",
    instructions=(
        "Operate the user's DreamAgent account: build and deploy apps and bots "
        "(websites, Telegram bots, Discord bots, AI agents) and edit them via "
        "DreamAgent's AI agent.\n\n"
        "HOW EDITS WORK — you only describe the CHANGE:\n"
        "DreamAgent's AI automatically handles code, tests, rebuild, "
        "redeployment, and commits. Edit messages should contain ONLY the "
        "desired change — never 'run the build/tests and deploy', 'check the "
        "logs', tech-stack guidance, or implementation steps.\n"
        "GOOD: 'Replace the dashboard cards with a cleaner 2-column layout; "
        "keep the existing color palette.'\n\n"
        "ORCHESTRATION RULES:\n"
        "1. PROJECT SELECTION — if the user names a project but the ID is "
        "unknown: dreamagent_list_projects first, then act.\n"
        "2. NEW PROJECT — confirm first: present the name, type, final "
        "description, and which saved credential will be used (if several "
        "telegram/discord credentials are saved, ask which one) and WAIT "
        "for the user's explicit go-ahead — never create on the first "
        "mention. Then create_project → get_project_status (poll until "
        "ready/failed).\n"
        "3. EXISTING PROJECT EDIT — confirm project → check no edit is "
        "already running (get_edit_progress) → dreamagent_chat → monitor "
        "until complete → only then report success.\n"
        "4. ONE EDIT AT A TIME — never launch two AI modifications on the "
        "same project concurrently; check progress and wait instead.\n"
        "5. PERSISTENCE — completed edits are saved and committed "
        "automatically; you may tell the user changes are saved.\n"
        "6. DEPLOYMENT — rebuild/redeploy is automatic after successful "
        "edits; report the project as updated only after completion.\n"
        "7. CREDENTIALS — raw tokens are NEVER accepted in chat or as tool "
        "inputs. Check dreamagent_list_global_integrations first and pass "
        "saved-credential IDs (bot_token_integration_id for bot tokens, "
        "global_integration_ids for other keys). If nothing is saved, direct "
        "the user to dreamagent.cloud → Settings → Global Integrations — "
        "never ask for pasted tokens. Global Integrations (user-level, "
        "reusable) are distinct from project env vars (per-project).\n"
        "8. ERRORS — if a tool reports no credits, an active edit, auth "
        "failure, or a lock, explain that user-facing issue; never pretend "
        "the operation completed.\n\n"
        "TOOL SELECTION EXAMPLES (intent → tool):\n"
        "- 'Show my DreamAgent projects' → list_projects\n"
        "- 'Create a Telegram customer support bot' → create_project "
        "(with saved-credential ID) → get_project_status until ready\n"
        "- 'Is my project ready?' → get_project_status\n"
        "- 'Add a /status command to Crypto Copilot' → list_projects (find "
        "ID) → get_edit_progress (no active edit) → dreamagent_chat → "
        "monitor until complete\n"
        "- 'Is that edit finished?' → get_edit_progress (or "
        "get_chat_status with the session key)\n"
        "- 'Stop the current edit' → cancel_chat (explicit request only)\n"
        "- 'Continue the previous development conversation' → "
        "list_sessions → dreamagent_chat with that session key\n"
        "- 'Start a separate development topic' → create_session\n"
        "- 'The project is locked but nothing is running' → "
        "release_project_lock, only after confirming the lock is stale\n"
    ),
)

# OAuth 2.1 authorization server routes (ChatGPT/Claude "Connect" flow):
# /register (DCR), /authorize, /token, and the /.well-known discovery docs.
register_oauth(mcp)


# Health endpoint for MCP gateways/directories (Glama & co. probe GET /health).
@mcp.custom_route("/health", methods=["GET"])
async def health_endpoint(_request):
    from starlette.responses import JSONResponse
    return JSONResponse({"status": "healthy", "service": "dreamagent-mcp"})


# OpenAI apps domain verification (developers.openai.com plugin submission).
# Serves the challenge token at the origin-root well-known URL:
#   https://mcp.dreamagent.cloud/.well-known/openai-apps-challenge
# Override with OPENAI_APPS_CHALLENGE_TOKEN if OpenAI re-issues a token.
_OPENAI_APPS_CHALLENGE_TOKEN = os.getenv(
    "OPENAI_APPS_CHALLENGE_TOKEN",
    "kg-v0lpsx3LeONG6yV6MWOr3ViPnz3sgeZAeMGilgpM",
)


@mcp.custom_route("/.well-known/openai-apps-challenge", methods=["GET"])
async def openai_apps_challenge(_request):
    from starlette.responses import PlainTextResponse
    return PlainTextResponse(_OPENAI_APPS_CHALLENGE_TOKEN)

_client: DreamAgentClient | None = None
_local_auth: AuthManager | None = None  # set only when env credentials exist


def client() -> DreamAgentClient:
    """Return the shared client.

    Hosted mode (default): no DREAMAGENT_EMAIL/PASSWORD — every tool call
    authenticates with the Bearer token from the incoming MCP request
    (the user's DreamAgent API key, set by ChatGPT's connector settings).
    Local mode: env credentials log in once and the token is cached.
    """
    global _client, _local_auth
    if _local_auth is None and _client is None:
        # First call: try env credentials; fall back to hosted (header) mode.
        import os
        if os.getenv("DREAMAGENT_EMAIL") and os.getenv("DREAMAGENT_PASSWORD"):
            try:
                _local_auth = AuthManager.from_env()
            except AuthError:
                _local_auth = None
        if _client is None:
            auth = _local_auth or AuthManager(
                api_url=os.getenv("DREAMAGENT_API_URL", "https://api.dreamagent.cloud"),
                email="", password="",
            )
            _client = DreamAgentClient(auth)
    return _client


def _bind_request_token() -> None:
    """Bind the incoming MCP request's credential (hosted mode).

    Called at the top of every tool. Accepted credential sources:
      1. Authorization: Bearer <da_...> header — OAuth clients (ChatGPT
         Connect flow, Claude connectors). NOTE: fastmcp's get_http_headers()
         STRIPS the Authorization header, so we read it from the raw
         starlette request instead.
      2. ?key=<da_...> URL query parameter — ChatGPT custom connectors with
         "No authentication" don't send headers, so the key rides in the
         server URL: https://mcp.dreamagent.cloud/mcp?key=da_...
    If neither is present and no env credentials exist, tools raise a
    friendly connect-your-account error on first API use.
    """
    token = None
    try:
        from fastmcp.server.dependencies import get_http_request
        request = get_http_request()
        if request is not None:
            auth_header = request.headers.get("authorization") or ""
            parts = auth_header.split()
            if len(parts) == 2 and parts[0].lower() == "bearer":
                token = parts[1]
            if not token:
                token = request.query_params.get("key") or None
    except Exception:
        pass
    set_request_token(token)


def _err(e: Exception) -> str:
    # Hosted mode with no token at all: give the connect instructions.
    if isinstance(e, AuthError) and not get_request_token() and _local_auth is None:
        return (
            "ERROR: No DreamAgent account connected. Add your DreamAgent API key "
            "to the connector settings in ChatGPT (create one at dreamagent.cloud → "
            "Settings → Connect to ChatGPT). Help: https://dreamagent.cloud/help"
        )
    return f"ERROR: {e}"


FREE_PLAN_MSG = (
    "ERROR: This MCP action is a Pro feature. File editing and publishing "
    "via MCP are available on paid plans — upgrade at "
    "https://dreamagent.cloud/pricing (free users can still read files and "
    "use the in-app code editor)."
)


def _require_paid() -> str | None:
    """Return FREE_PLAN_MSG when the connected account is on the free plan.

    Call inside write/delete tools AFTER _bind_request_token(); returning
    a non-None value means the action must be refused.
    """
    try:
        return FREE_PLAN_MSG if client().get_plan_slug() == "free" else None
    except Exception:
        # Plan lookup failing must not block paying users — fail open;
        # the downstream API still enforces ownership and credits.
        return None


# ---------------------------------------------------------------------------
# Tool 1 — list projects
# ---------------------------------------------------------------------------

@mcp.tool(annotations=_READ_ONLY)
def dreamagent_list_projects(status: str | None = None) -> str:
    """
    List the user's DreamAgent projects.

    Returns each project's name, ID, type, status, and live domain (when
    available). Use when the user asks to see, find, select, or check their
    projects — and ALWAYS before modifying a project when its ID is not
    already known (resolve names to IDs here first).

    Args:
        status: optional exact-match filter (e.g. 'ready', 'failed').
            During creation, projects report intermediate phases
            (creating/building/deploying/…), so filtering by 'creating'
            does not match every in-progress project.
"""
    try:
        _bind_request_token()
        projects = client().list_projects(status)
    except (AuthError, DreamAgentAPIError) as e:
        return _err(e)
    except Exception:
        return "ERROR: internal error. Please retry — if it persists, check dreamagent.cloud status."

    if not projects:
        return "No projects found." if not status else f"No projects with status '{status}'."

    type_names = {v: k for k, v in PROJECT_TYPES.items()}
    lines = []
    for p in projects:
        lines.append(
            f"#{p.get('id')} [{type_names.get(p.get('type_id'), p.get('type_id'))}] "
            f"\"{p.get('name')}\" — status: {p.get('status')}, "
            f"domain: {p.get('domain')}, created: {p.get('created_at')}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool 2 — create project
# ---------------------------------------------------------------------------

@mcp.tool(annotations=_READ_ONLY)
def dreamagent_list_global_integrations() -> str:
    """
    List the user's saved credentials (Global Integrations).

    ALWAYS call this before creating any bot project or asking the user
    for a token/API key. Saved credentials are referenced BY ID when
    creating projects: bot_token_integration_id for bot tokens,
    global_integration_ids for other keys. Secret values are never
    returned — only IDs and metadata (type, key name, verified, title).

    If nothing suitable is saved: direct the user to dreamagent.cloud →
    Settings → Global Integrations to add it (one-time, verified,
    reusable). Never ask the user to paste tokens in chat.
"""
    try:
        _bind_request_token()
        items = client().list_global_integrations()
    except (AuthError, DreamAgentAPIError) as e:
        return _err(e)
    except Exception:
        return "ERROR: internal error. Please retry — if it persists, check dreamagent.cloud status."

    # Connected OAuth services (Nango) — agents can use these for
    # YouTube, GitHub, Discord, Notion, X, Google Sheets, Slack actions
    # without any credential ids. Fail-soft: empty when Nango is off.
    oauth_connected: list[str] = []
    try:
        for p in client().list_oauth_providers():
            if not p.get("connected"):
                continue
            name = p.get("title") or p.get("provider", "?")
            accounts = p.get("connected_accounts") or []
            if len(accounts) > 1:
                labels = ", ".join(
                    f'"{a.get("label")}"' + (" (default)" if a.get("is_default") else "")
                    for a in accounts)
                oauth_connected.append(f"{name} — accounts: {labels}")
            else:
                oauth_connected.append(name)
    except Exception:
        pass

    oauth_note = ""
    if oauth_connected:
        oauth_note = ("\n\nConnected OAuth services (usable by AI agents and "
                      "projects, no credential id needed): "
                      + "; ".join(sorted(oauth_connected))
                      + ". Providers listing multiple accounts: target one "
                        "via the proxy's `account` parameter (label).")
    elif items:
        oauth_note = ("\n\nNo OAuth services connected yet. Agents that need "
                      "YouTube/GitHub/Discord/Notion/X/Sheets/Slack actions "
                      "should direct the user to dreamagent.cloud → Integrations.")

    if not items:
        base = ("No saved credentials yet. The user must add them at "
                "dreamagent.cloud → Settings → Global Integrations "
                "(one-time, verified, reusable). Tokens are never accepted "
                "in chat — wait for them to save it, then call this tool again. "
                "Help: https://dreamagent.cloud/help")
        return base + oauth_note
    lines = []
    for gi in items:
        v = " ✓verified" if gi.get("verified") else ""
        lines.append(
            f"#{gi.get('id')} [{gi.get('token_type')}] {gi.get('key_name')} — "
            f"{gi.get('title') or 'no title'}{v}"
        )
    return ("\n".join(lines)
            + "\nUse these ids in dreamagent_create_project instead of asking "
              "the user to paste tokens." + oauth_note)


@mcp.tool(annotations=_READ_ONLY)
def dreamagent_list_project_env(project_id: int) -> str:
    """
    List a project's environment variables with their details.

    Shows each key's name, title, description, docs URL, and category —
    values are masked (secrets are never returned). Use to see which
    integrations a project already has before adding more, or to answer
    "what keys does my project have?".

    These are PROJECT-SPECIFIC variables — distinct from the user's
    Global Integrations — API-key credentials and connected OAuth services (dreamagent_list_global_integrations).

    Args:
        project_id: the project to inspect.
"""
    try:
        _bind_request_token()
        data = client().list_project_env(project_id)
    except (AuthError, DreamAgentAPIError) as e:
        return _err(e)
    except Exception:
        return "ERROR: internal error. Please retry — if it persists, check dreamagent.cloud status."

    variables = data.get("variables") or []
    if not variables:
        return f"Project {project_id} has no environment variables configured."
    lines = [f"Project {data.get('project_name', project_id)} env keys:"]
    for v in variables:
        parts = [f"{v.get('key')}"]
        if v.get("title"):
            parts.append(f"— {v.get('title')}")
        if v.get("category"):
            parts.append(f"[{v.get('category')}]")
        if v.get("is_sensitive") or v.get("masked"):
            parts.append("(sensitive, value hidden)")
        lines.append("  " + " ".join(parts))
        if v.get("description"):
            lines.append(f"      {v.get('description')}")
        if v.get("docs_url"):
            lines.append(f"      docs: {v.get('docs_url')}")
    return "\n".join(lines)


@mcp.tool(annotations=_READ_ONLY)
def dreamagent_list_superpowers() -> str:
    """
    List DreamAgent Superpowers — the account's ready-made AI tools
    (song/video/image generation, voiceover, lip sync, voice clone,
    transcription, media processing, PDF tools) with their enabled
    state and beta pricing. Read-only; no credits consumed.

    Use when the user asks what AI tools/superpowers are available,
    whether a specific tool is enabled, or what they can add to a
    project. Tools shown as enabled can be used from ANY of the
    user's projects through dreamagent_chat (just ask in the message).
    """
    try:
        _bind_request_token()
        tools = client().list_superpowers()
    except (AuthError, DreamAgentAPIError) as e:
        return _err(e)
    except Exception:
        return "ERROR: internal error. Please retry — if it persists, check dreamagent.cloud status."
    if not tools:
        return "No superpowers found."
    lines = []
    for t in tools:
        state = "enabled" if t.get("enabled") else "disabled"
        price = ""
        if t.get("category") == "paid":
            price = f" — {t.get('credit_cost_beta')} credits (beta) per {t.get('credit_unit', 'use')}"
        avail = "" if t.get("available") else " [coming soon]"
        lines.append(f"- {t['name']} ({state}{price}){avail}: {t['description']}")
    enabled_count = sum(1 for t in tools if t.get("enabled"))
    return (f"Superpowers ({enabled_count}/{len(tools)} enabled):\n"
            + "\n".join(lines))


@mcp.tool(annotations=_READ_ONLY)
def dreamagent_list_files(project_id: int) -> str:
    """
    List the file tree of a DreamAgent project (source files only —
    hidden files like .env are excluded). Use before reading or editing
    a file to find the correct path.
    """
    try:
        _bind_request_token()
        tree = client().list_files(project_id)
    except (AuthError, DreamAgentAPIError) as e:
        return _err(e)
    except Exception:
        return "ERROR: internal error. Please retry."

    lines = []
    def walk(nodes, depth):
        for n in nodes:
            kind = 'dir ' if n.get('type') == 'folder' else 'file'
            size = n.get('size')
            lines.append(f"{'  ' * depth}[{kind}] {n['path']}" + (f" ({size}B)" if kind == 'file' and size is not None else ""))
            if n.get('children'):
                walk(n['children'], depth + 1)
    walk(tree, 0)
    if not lines:
        return "No files found."
    return f"Project {project_id} files:\n" + "\n".join(lines[:200])


@mcp.tool(annotations=_READ_ONLY)
def dreamagent_read_file(project_id: int, file_path: str) -> str:
    """
    Read a source file from a DreamAgent project. file_path is the
    project-relative path (from dreamagent_list_files), e.g.
    "src/pages/Home.tsx" or "telegram/main.py". Hidden files (.env) and
    binaries are not readable.
    """
    try:
        _bind_request_token()
        data = client().read_file(project_id, file_path)
    except (AuthError, DreamAgentAPIError) as e:
        return _err(e)
    except Exception:
        return "ERROR: internal error. Please retry."
    if data.get("is_binary"):
        return f"'{file_path}' is a binary file ({data.get('size')} bytes) — content not shown."
    content = data.get("content") or ""
    return f"'{file_path}' ({data.get('size')} bytes):\n{content}"


def _commit_project(project_id: int, message: str) -> str:
    """Commit+push all pending changes of a project and record a system
    entry in its 'ChatGPT' session history + version history (commit_log).
    Never raises — returns a short status line instead."""
    try:
        c = client()
        session = c.ensure_chatgpt_session(project_id)
        session_id = int((session or {}).get("id"))
        r = c.commit_and_push(project_id, session_id, message[:80])
    except Exception as e:
        detail = f" ({e})" if isinstance(e, (AuthError, DreamAgentAPIError)) else ""
        return (f"COMMIT FAILED{detail} — the edit itself IS saved; retry with "
                "dreamagent_commit_push.")
    if not r.get("success"):
        return (f"COMMIT FAILED ({r.get('error') or r.get('status')}) — the edit "
                "itself IS saved; retry with dreamagent_commit_push.")
    h = (r.get("commit_hash") or "")[:8]
    where = "Recorded in session history & version history (restorable)."
    if r.get("status") == "pushed":
        return f"Committed {h} and pushed to GitHub. {where}"
    return f"Committed {h} (push status: {r.get('status')}). {where}"


def _apply_edits(content: str, edits) -> tuple:
    """Apply exact-match find→replace edits to a file's content.

    Returns (new_content, notes). Raises ValueError with a precise message
    when an edit cannot apply — the caller must not save in that case.
    """
    notes = []
    for i, e in enumerate(edits, 1):
        if not isinstance(e, dict):
            raise ValueError(f"edit {i}: must be an object with find/replace")
        find = e.get("find")
        repl = e.get("replace", "")
        replace_all = bool(e.get("all"))
        if not isinstance(find, str) or not find:
            raise ValueError(f"edit {i}: 'find' must be a non-empty string")
        if not isinstance(repl, str):
            raise ValueError(f"edit {i}: 'replace' must be a string")
        if find == repl:
            raise ValueError(f"edit {i}: 'find' and 'replace' are identical")
        count = content.count(find)
        if count == 0:
            raise ValueError(
                f"edit {i}: 'find' text NOT found in the file — copy it exactly "
                "from a fresh dreamagent_read_file (whitespace and indentation "
                "must match)")
        if count > 1 and not replace_all:
            raise ValueError(
                f"edit {i}: 'find' text appears {count} times — include more "
                "surrounding lines to make it unique, or set \"all\": true to "
                "replace every occurrence")
        content = content.replace(find, repl) if replace_all \
            else content.replace(find, repl, 1)
        notes.append(f"edit {i}: replaced {'all ' + str(count) if replace_all else '1'}"
                     f" occurrence(s) ({len(find)} → {len(repl)} chars)")
    return content, notes


@mcp.tool(annotations=_WRITES_PROJECT)
def dreamagent_edit_file(project_id: int, file_path: str, edits: list,
                         confirm: bool = False, auto_commit: bool = True) -> str:
    """
    WRITE ACTION — make SMALL targeted changes to an EXISTING file in a
    DreamAgent project via exact search/replace. PREFERRED over
    dreamagent_write_file for editing existing files: you send only the
    changed fragments (never the whole file) — cheaper, immune to payload
    size/safety blocks in the calling client, and cannot corrupt unchanged
    code. Use dreamagent_write_file only to CREATE files or for full rewrites.

    Read the file first (dreamagent_read_file) and copy each 'find' text
    EXACTLY as it appears — whitespace and indentation must match. All edits
    are validated BEFORE anything is saved; if any 'find' fails to match,
    the file is left untouched and the error tells you which edit and why.

    CONFIRM BEFORE CALLING — show the user the target file and each planned
    find→replace change, then call only with confirm=true.

    Args:
        project_id: project containing the file.
        file_path: project-relative path of the EXISTING file to edit.
        edits: list of edit objects:
            {"find": "<exact existing text>",
             "replace": "<new text>",
             "all": false}   # optional: replace every occurrence
        confirm: must be true to execute.
        auto_commit: commit+push and record version history after saving
            (default true; pass false to batch several edit_file calls and
            finish with one dreamagent_commit_push).
    """
    try:
        _bind_request_token()
        gate = _require_paid()
        if gate:
            return gate
        if not confirm:
            n = len(edits) if isinstance(edits, list) else 0
            return (f"CONFIRMATION REQUIRED — about to apply {n} edit(s) to "
                    f"'{file_path}' in project {project_id}. Show the user each "
                    "find→replace change, then call again with confirm=true.")
        if not isinstance(edits, list) or not edits:
            return "ERROR: edits must be a non-empty list of {find, replace} objects."
        c = client()
        data = c.read_file(project_id, file_path)
        if data.get("is_binary"):
            return f"ERROR: '{file_path}' is a binary file — cannot edit."
        content = data.get("content") or ""
        new_content, notes = _apply_edits(content, edits)
        c.save_file(project_id, file_path, new_content)
    except (AuthError, DreamAgentAPIError) as e:
        return _err(e)
    except ValueError as e:
        return f"ERROR: {e}"
    except Exception as e:
        logger.exception("edit_file failed for project %s", project_id)
        return f"ERROR: {type(e).__name__}: {e}. Please retry."
    size_before = len(content.encode("utf-8"))
    size_after = len(new_content.encode("utf-8"))
    out = (f"Edited '{file_path}' in project {project_id} — {len(notes)} edit(s) "
           f"applied, {size_before} → {size_after} bytes:\n"
           + "\n".join(f"- {n}" for n in notes))
    if auto_commit:
        out += "\n" + _commit_project(
            project_id, f"MCP edit: {file_path} ({len(notes)} edits)")
    else:
        out += ("\nNot committed yet — call dreamagent_commit_push when the "
                "batch is done.")
    return out


@mcp.tool(annotations=_WRITES_PROJECT)
def dreamagent_write_file(project_id: int, file_path: str, content: str,
                          confirm: bool = False, auto_commit: bool = True) -> str:
    """
    WRITE ACTION — overwrite or create one source file in a DreamAgent
    project with the exact content given. For CHANGING an existing file,
    prefer dreamagent_edit_file (small targeted find→replace edits — no
    need to send the whole file). By default the write is
    immediately committed and pushed to the project's GitHub repo and
    recorded in its session history & version history (so the user can
    restore/revert). Changes go live after the project is rebuilt
    (dreamagent_build_publish).

    CONFIRM BEFORE CALLING — in ONE message show the user: the target
    path and a summary of the change, then call only after they agree.
    Never write on the first mention.

    MULTI-FILE EDITS: pass auto_commit=false on each write, then call
    dreamagent_commit_push once at the end — one commit and one history
    entry for the whole batch.

    GUARDS (enforced server-side, the same as the in-app code editor):
    path stays inside the project, hidden/credential files (.env, .git,
    keys) are rejected, content is scanned for malware/shell signatures
    and blocked, 2MB size cap. Failed scans return the reason.

    The user can undo damage by editing in the DreamAgent editor or
    restoring a previous version from version history — still, prefer
    precise minimal diffs.
    """
    try:
        _bind_request_token()
        gate = _require_paid()
        if gate:
            return gate
        if not confirm:
            return (
                f"CONFIRMATION REQUIRED — about to overwrite '{file_path}' in "
                f"project {project_id} with {len(content)} characters of new content. "
                "Show the user the target path and the change summary, and only call "
                "again with confirm=true after they agree.")
        result = client().save_file(project_id, file_path, content)
    except (AuthError, DreamAgentAPIError) as e:
        return _err(e)
    except Exception:
        return "ERROR: internal error. Please retry."
    size = result.get('size', len(content))
    out = (f"Saved '{file_path}' in project {project_id} ({size} bytes). "
           "Changes are not live until the project is rebuilt — call "
           "dreamagent_build_publish when the user is ready.")
    if auto_commit:
        out += "\n" + _commit_project(project_id, f"MCP write: {file_path}")
    else:
        out += ("\nNot committed yet — finish the batch, then call "
                "dreamagent_commit_push for one commit of everything.")
    # blocked writes come back inside the error path of save_file
    return out


@mcp.tool(annotations=_READ_ONLY)
def dreamagent_get_file_diff(project_id: int, file_path: str) -> str:
    """
    Git diff of one project file against the last commit — shows the
    pending (not yet live) changes. Free, read-only. Use after edits to
    review exactly what changed before rebuilding.
    """
    try:
        _bind_request_token()
        d = client().get_file_diff(project_id, file_path)
    except (AuthError, DreamAgentAPIError) as e:
        return _err(e)
    except Exception:
        return "ERROR: internal error. Please retry."
    return f"Diff for '{d.get('path')}' vs last commit:\n{d.get('diff')}"


@mcp.tool(annotations=_WRITES_PROJECT)
def dreamagent_delete_file(project_id: int, file_path: str,
                           confirm: bool = False, auto_commit: bool = True) -> str:
    """
    WRITE ACTION — delete one source file from a DreamAgent project.
    Hidden/credential files (.env, .git, keys) are rejected. By default the
    deletion is immediately committed and pushed (so it is recorded in
    session history & version history) — deleted files stay recoverable
    via git history / version restore.

    CONFIRM BEFORE CALLING — tell the user which file will be deleted and
    call only with confirm=true after they agree.
    """
    try:
        _bind_request_token()
        gate = _require_paid()
        if gate:
            return gate
        if not confirm:
            return (f"CONFIRMATION REQUIRED — about to DELETE '{file_path}' in "
                    f"project {project_id}. Call again with confirm=true after "
                    "the user agrees.")
        result = client().delete_file(project_id, file_path)
    except (AuthError, DreamAgentAPIError) as e:
        return _err(e)
    except Exception:
        return "ERROR: internal error. Please retry."
    out = (f"Deleted '{file_path}' from project {project_id}. Recoverable via "
           "git history. Rebuild to apply.")
    if auto_commit:
        out += "\n" + _commit_project(project_id, f"MCP delete: {file_path}")
    else:
        out += ("\nNot committed yet — call dreamagent_commit_push when the "
                "batch of edits is done.")
    return out


@mcp.tool(annotations=_WRITES_PROJECT)
def dreamagent_commit_push(project_id: int, message: str = "",
                           confirm: bool = False) -> str:
    """
    WRITE ACTION — commit ALL pending changes in a DreamAgent project and
    push them to its private GitHub repo. The commit gets its own entry in
    the project's session history and version history, so the user can
    restore/revert to it in the DreamAgent dashboard.

    Use it to (a) commit several MCP writes/deletes as ONE version — write
    and delete with auto_commit=false first — or (b) retry a commit after a
    failure. Chat-driven edits commit automatically; this tool is for
    direct file changes made through MCP.

    CONFIRM BEFORE CALLING — show the user what is pending (one
    dreamagent_get_file_diff per touched file) and call only with
    confirm=true after they agree.

    Args:
        project_id: project to commit
        message: short commit message, e.g. "Add pricing page hero section"
            (default "MCP edit: project <id>")
        confirm: must be true to execute
    """
    try:
        _bind_request_token()
        gate = _require_paid()
        if gate:
            return gate
        if not confirm:
            return (f"CONFIRMATION REQUIRED — about to commit & push ALL pending "
                    f"changes in project {project_id}. Show the user what changed "
                    "(dreamagent_get_file_diff), then call again with confirm=true.")
        note = _commit_project(project_id,
                               message or f"MCP edit: project {project_id}")
    except (AuthError, DreamAgentAPIError) as e:
        return _err(e)
    except Exception:
        return "ERROR: internal error. Please retry."
    return f"Project {project_id}: {note}"


@mcp.tool(annotations=_READ_ONLY)
def dreamagent_list_commits(project_id: int, limit: int = 10) -> str:
    """
    Version history of a DreamAgent project — recent commits (hash,
    message, status, timestamp) from its persistent commit log, newest
    first. Use it to review what changed and when; the user can restore
    any version from the DreamAgent dashboard version-history UI (mention
    the short hash so they can find it).
    """
    try:
        _bind_request_token()
        data = client().list_commits(project_id, limit=max(1, min(50, int(limit))))
    except (AuthError, DreamAgentAPIError) as e:
        return _err(e)
    except Exception:
        return "ERROR: internal error. Please retry."
    commits = data.get("commits") or []
    if not commits:
        return f"Project {project_id} has no commits yet."
    lines = []
    for c in commits:
        msg = (c.get("commit_message") or "").splitlines()[0][:60]
        when = (c.get("created_at") or "")[:16]
        lines.append(f"{(c.get('commit_hash') or '')[:8]}  "
                     f"[{c.get('commit_status')}]  {msg}  ({when})")
    repo = data.get("repo_url") or "unknown repo"
    return ("Version history for project {0} (newest first):\n{1}\nRepo: {2}"
            .format(project_id, "\n".join(lines), repo))


@mcp.tool(annotations=_WRITES_PROJECT)
def dreamagent_build_publish(project_id: int, confirm: bool = False) -> str:
    """
    WRITE ACTION — build & publish a DreamAgent project: rebuilds the
    app from the current source files and pushes the new version live.
    Use after finishing file edits (dreamagent_write_file) so the user's
    changes go live. Pro/paid accounts only.

    CONFIRM BEFORE CALLING — tell the user the project will be rebuilt
    and redeployed, and call only with confirm=true after they agree.

    ASYNCHRONOUS: kick-off returns immediately; check
    dreamagent_get_project_status until the project reports 'ready'.
    """
    try:
        _bind_request_token()
        gate = _require_paid()
        if gate:
            return gate
        if not confirm:
            return (
                f"CONFIRMATION REQUIRED — build & publish project {project_id}? "
                "The current files will be rebuilt and pushed live. Call again "
                "with confirm=true after the user agrees.")
        result = client().build_publish(project_id)
    except (AuthError, DreamAgentAPIError) as e:
        return _err(e)
    except Exception:
        return "ERROR: internal error. Please retry."
    return (
        f"Build & publish started for project {project_id}. Poll "
        f"dreamagent_get_project_status(project_id={project_id}) until it "
        "reports 'ready', then share the live URL.")


@mcp.tool(annotations=_WRITES_PROJECT)
def dreamagent_design_mode(project_id: int, image_base64: str, instructions: str,
                           confirm: bool = False) -> str:
    """
    WRITE ACTION — redesign a website project's UI to match a design image.
    Website projects only.

    The user provides an image (screenshot, mockup, AI-generated design)
    and instructions. You generate the image (e.g. DALL-E), encode it as
    base64, and send it here. DreamAgent's AI will read the image and
    redesign the project's frontend to match.

    CONFIRM BEFORE CALLING — show the user:
    1. The project being redesigned
    2. The design instructions
    Then call only with confirm=true after they agree.

    Args:
        project_id: the website project to redesign.
        image_base64: base64-encoded design reference image (PNG/JPG/WEBP).
            Send the actual image bytes verbatim — the image IS the spec.
        instructions: keep this SHORT — e.g. "Match this design image exactly."
            The image carries the design; do NOT write a long textual design
            spec duplicating what is visible in the image. Only add brief
            must-have requirements the image cannot show (e.g. "keep all
            existing pages and functionality", "Product Hunt view only").
        confirm: must be true to execute.
"""
    try:
        _bind_request_token()
        gate = _require_paid()
        if gate:
            return gate
        if not confirm:
            return (
                f"CONFIRMATION REQUIRED — redesign project {project_id} to match "
                f"the provided design image. Instructions: {instructions[:200]}. "
                "Call again with confirm=true after the user agrees.")
        import base64 as b64
        payload = image_base64.strip()
        if payload.startswith("data:"):        # tolerate data URI prefixes
            payload = payload.split(",", 1)[-1]
        payload = "".join(payload.split())      # tolerate whitespace/newlines
        raw, decode_err = None, None
        for cand in (payload, payload.replace("-", "+").replace("_", "/")):
            try:
                raw = b64.b64decode(cand + "=" * (-len(cand) % 4))
                break
            except Exception as e:
                decode_err = e
        if raw is None:
            logger.warning("design_mode p%s: base64 decode failed (%s); payload len=%d head=%.40s",
                           project_id, decode_err, len(payload), payload[:40])
            return (f"ERROR: image_base64 failed to decode ({decode_err}; payload "
                    f"length {len(payload)} — a much shorter length than the original "
                    "image suggests truncation; re-encode the file contents).")
        # format detection (accept anything image-like; reject text/paths)
        if raw[:8].startswith(b"\x89PNG"):
            ext = "png"
        elif raw[:3] == b"\xff\xd8\xff":
            ext = "jpg"
        elif raw[:4] == b"GIF8":
            ext = "gif"
        elif raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
            ext = "webp"
        elif raw[4:8] == b"ftyp":               # AVIF/HEIC ISO-BMFF
            ext = "img"
        elif raw[:5] == b"<?xml" or raw.lstrip()[:4] == b"<svg":
            ext = "svg"
        elif not all(32 <= c < 127 or c in (9, 10, 13) for c in raw[:300]):
            ext = "img"                          # unknown binary — let the backend scan it
        else:
            logger.warning("design_mode p%s: decoded payload is TEXT not image bytes (head=%.60s)",
                           project_id, raw[:60])
            return ("ERROR: image_base64 decoded to TEXT, not image bytes — read the "
                    "image file and base64-encode its actual contents (never a path).")
        if len(raw) > 10 * 1024 * 1024:
            return "ERROR: image too large (>10 MB) — use a smaller design reference."
        image_bytes = raw
        filename = f"design_ref_{project_id}.{ext}"
        c = client()
        upload = c.upload_file(project_id, filename, image_bytes)
        # The backend renames+converts uploads (design_ref_2043.png ->
        # image_2043_<ts>.webp) and returns NO 'path' key — derive the real
        # project-relative path so the agent can actually find the file.
        rel = upload.get("path") or upload.get("relative_path")
        if not rel:
            cp = upload.get("container_path") or ""
            m = re.search(rf"/{project_id}_[^/]+/(.+)$", cp)
            rel = m.group(1) if m else (upload.get("url") or "").lstrip("/")
        web_url = upload.get("url") or ""
        design_message = (
            f"Read the design reference image file at the project path {rel}"
            + (f" (web URL: {web_url})" if web_url else "")
            + f" and redesign the entire frontend UI to match it. Requirements: "
            f"{instructions}. "
            "Analyze the layout, colors, typography, spacing, and components in "
            "the image. Apply the design across ALL pages. Rebuild and publish "
            "when done.")
        # resolve OUR 'ChatGPT' session → string session_key (the chat API
        # requires a string key; passing the raw project id fails validation)
        result = c.submit_design_chat(project_id, design_message)
    except (AuthError, DreamAgentAPIError) as e:
        return _err(e)
    except Exception as e:
        logger.exception("design_mode failed for project %s", project_id)
        return f"ERROR: {type(e).__name__}: {e}. Please retry."
    submitted = result.get("status") == "submitted"
    head = ("Design mode submitted for project "
            if submitted else
            "Design mode likely submitted for project "
            "(platform acknowledgement timed out — verify with progress): ")
    tail = ("" if submitted else
            " If no run is active, call this tool again.")
    return (f"{head}{project_id}. The agent is reading the design reference and "
            f"redesigning the UI. Poll dreamagent_get_edit_progress("
            f"project_id={project_id}) until done, then check the live site.{tail}")


@mcp.tool(annotations=_WRITES_PROJECT)
def dreamagent_create_project(
    name: str,
    project_type: str,
    description: str | None = None,
    env_vars: dict | None = None,
    bot_token_integration_id: int | None = None,
    global_integration_ids: list | None = None,
) -> str:
    """
    WRITE ACTION — creates a new DreamAgent project (website, Telegram
    bot, Discord bot, or AI agent). Use ONLY when the user clearly asks
    to create/build a new project; not for questions about what to build.

    CONFIRM BEFORE CALLING — never create on the first mention. In ONE
    message, present and get the user's explicit go-ahead for:
    1. The credential: run dreamagent_list_global_integrations first.
       If several telegram/discord credentials are saved, list them
       (id + title) and ask which to use. If exactly one is saved,
       state which one you will use. Never pick silently.
    2. The plan: name, type, and the final description exactly as you
       will submit it (this is the Prompt Assistant's recommendation
       summary). Call this tool only after the user confirms.

    CREATION IS ASYNCHRONOUS: after calling, check
    dreamagent_get_project_status repeatedly until the project is
    'ready' or 'failed' (bots ~2-5 min, websites longer).

    CREDENTIALS: raw bot tokens are never accepted in chat or as inputs.
    For Telegram/Discord bots you MUST pass bot_token_integration_id —
    the ID of a saved credential from dreamagent_list_global_integrations.
    If none is saved, direct the user to dreamagent.cloud → Settings →
    Global Integrations. Other saved keys can be imported via
    global_integration_ids. Credentials are stored as project secrets
    and are never exposed back.

    DESCRIPTION = the refined creation brief. Act as a Creative
    Director / Product Manager, not an architect.

    Create a concise but complete description of WHAT should be built:
    - product vision and target audience
    - user experience and core functionality
    - design/tone direction
    - important features and behavior
    - final expected result

    Do not include implementation details such as:
    - technology stack
    - architecture
    - database/API implementation
    - authentication implementation
    - deployment
    - CI/CD
    - testing commands

    Infer reasonable defaults when missing details are non-critical.
    Ask for clarification only when missing information materially
    affects the requested functionality, scope, credentials, or
    expected result.

    Prefer the following structure and constraints for each project
    type, but do not override explicit user requirements:

    Website:
    - Project Vision
    - Design Style
    - Pages
    - Hero Experience
    - Core Features
    - UI Components
    - Mobile Experience
    - Final Expectation
    - Prefer up to 4 pages unless the user clearly requires more.

    Telegram bot:
    - Bot Purpose
    - Commands
    - User Flow
    - Optional AI Features
    - Integrations when required
    - Final Expectations
    - Prefer a compact command set; always include /start and /help
      unless the user's explicit requirements conflict.

    Discord bot:
    - Bot Purpose
    - Slash Commands
    - Events
    - Permissions
    - Optional AI Features
    - Final Expectations
    - Prefer a compact command set; include /help where appropriate.

    Agent (recurring/scheduled automation with OAuth actions):
    - Purpose (what it monitors or automates)
    - Data Sources (which connected OAuth services: YouTube, GitHub,
      Discord, Notion, X, Google Sheets, Slack — or public APIs)
    - Schedule (e.g., every 5 minutes / hourly / daily at 9am) and/or
      Event Triggers (incoming webhook)
    - Conditions (when to act — thresholds, keywords, state changes)
    - Delivery Channels (Telegram, Discord, Slack, email — optional)
    - Final Expectation
    - Use concrete schedules; note that agents keep persistent state
      between runs and can call any connected OAuth integration.
    - Agents do NOT need a bot token unless they deliver via Telegram/
      Discord (then a saved credential is required as for bots).

    SUPERPOWERS: the account may have ready-made AI tools enabled
    (song/video/image generation, voiceover, lip sync, voice clone,
    transcription, media processing, PDF tools). If the requested
    product clearly benefits from one (e.g. an AI song generator
    website), reflect the FEATURE in the description — DreamAgent
    wires the tool integration automatically at build time. Check
    what is enabled with dreamagent_list_superpowers. Do NOT add
    implementation details about how the tool is called.

    Args:
        name: project name (max 30 chars; a public subdomain is auto-generated).
        project_type: website / telegrambot / discordbot / agent.
        bot_token_integration_id: REQUIRED for telegrambot & discordbot —
            saved-credential ID (see dreamagent_list_global_integrations).
        global_integration_ids: optional saved-key IDs to import as env vars.
        description: the build request (what + for whom + features/tone).
        env_vars: optional non-secret environment variables.
"""
    try:
        _bind_request_token()
        p = client().create_project(name, project_type, description,
                                    env_vars, bot_token_integration_id,
                                    global_integration_ids)
    except (AuthError, DreamAgentAPIError) as e:
        return _err(e)
    except Exception:
        return "ERROR: internal error. Please retry — if it persists, check dreamagent.cloud status."

    return (
        f"Project #{p.get('id')} \"{p.get('name')}\" ({project_type}) queued for creation. "
        f"Status: {p.get('status')}. Domain will be: {p.get('domain')}. "
        f"Poll dreamagent_get_project_status(project_id={p.get('id')}) until status is 'ready'."
    )


# ---------------------------------------------------------------------------
# Tool 3 — project status
# ---------------------------------------------------------------------------

@mcp.tool(annotations=_READ_ONLY)
def dreamagent_get_project_status(project_id: int) -> str:
    """
    Check a project's creation/deployment state. Terminal states are
    'ready' (built and live) and 'failed'; while a project is being
    created it reports progressive phases such as 'creating',
    'building', 'deploying', or 'ai_provisioning' — treat any
    non-terminal status as still building. Use after
    dreamagent_create_project (poll until ready or failed) and
    whenever the user asks whether a project is ready or live.

    NOTE: this reports the PROJECT's build/deploy state — NOT AI edit
    progress. For edits use dreamagent_get_edit_progress.

    Args:
        project_id: the project to check.
"""
    try:
        _bind_request_token()
        s = client().get_project_status(project_id)
    except (AuthError, DreamAgentAPIError) as e:
        return _err(e)
    except Exception:
        return "ERROR: internal error. Please retry — if it persists, check dreamagent.cloud status."

    status = s.get("status")
    if status == "ready":
        return (f"Project {project_id} is READY and live. "
                f"(Find its URLs via dreamagent_list_projects.)")
    if status == "failed":
        return (f"Project {project_id} FAILED to build. Check the project in the "
                f"dreamagent.cloud dashboard (logs tab) for the error.")
    return (f"Project {project_id} status: {status} (still building). "
            f"Poll again in ~30 seconds with dreamagent_get_project_status.")


# ---------------------------------------------------------------------------
# Tool 4 — chat (edit/build via the AI agent)
# ---------------------------------------------------------------------------

@mcp.tool(annotations=_WRITES_PROJECT)
def dreamagent_chat(project_id: int, message: str,
                    session_key: str | None = None, new_session: bool = False) -> str:
    """
    WRITE ACTION — builds, modifies, or fixes an EXISTING project with
    a natural-language instruction. Use ONLY when the user clearly asks
    for a change ("add X", "fix Y", "change the theme"). If the user is
    only asking for advice, analysis, review, or suggestions ("what
    could be improved?", "review my bot"), do NOT call this — answer
    from the project's info instead; a review request is not an edit.

    Describe ONLY the desired change — DreamAgent's AI automatically
    handles code, tests, rebuild, and redeployment. Completed changes
    are saved and committed automatically.

    ASYNCHRONOUS: returns immediately with the session_key; monitor
    with dreamagent_get_edit_progress (or dreamagent_get_chat_status
    with the returned session key) until a terminal state. Report
    success only after run_status reaches 'completed'. Edits consume
    the user's AI credits.

    ERRORS (actual returns): failures come back as text starting with
    'ERROR:'. If the edit was rejected before starting, the text says
    'the edit was NOT started' plus the reason — HTTP 402 insufficient
    credits, 409 another edit already active, 423 session lock held by
    another session, 404 wrong project id, 401 no account connected.
    Treat the edit as failed/cancelled only when a status tool says so.

    ONE EDIT AT A TIME: never start another modification on the same
    project while one is running — check progress first and wait.

    SUPERPOWERS: projects can use the account's enabled AI tools
    (song/video/image generation, voiceover, lip sync, voice clone,
    transcription, media processing, PDF tools). If the user asks to
    add or use one ("add song generation", "generate an image feature"),
    simply include that in the message — DreamAgent's agent knows the
    enabled tools and wires the integration, UI and job handling
    automatically. Check the catalog with dreamagent_list_superpowers;
    if a tool is disabled, direct the user to dreamagent.cloud ->
    Superpowers to enable it first.

    Args:
        project_id: the project to modify.
        message: the desired change (what + any behavioral constraints).
        session_key: optional — continue a specific development session.
        new_session: optional — start a fresh session for a new topic.
"""
    try:
        _bind_request_token()
        c = client()

        if session_key:
            pass  # continue the requested session (ownership checked server-side)
        elif new_session:
            session = c.create_session(project_id, label="ChatGPT")
            session_key = session["session_key"]
        else:
            session = c.ensure_chatgpt_session(project_id)
            session_key = session["session_key"]

        early_error = c.submit_chat(session_key, message)
        if early_error:
            return (f"ERROR: the edit was NOT started: {early_error}\n"
                    f"(session_key={session_key})")
    except (AuthError, DreamAgentAPIError) as e:
        return _err(e)
    except Exception:
        return "ERROR: internal error. Please retry — if it persists, check dreamagent.cloud status."

    return (
        f"Edit submitted to project {project_id} (session_key={session_key}). "
        f"The agent is now working. Poll dreamagent_get_chat_status("
        f"session_key=\"{session_key}\") — pass back the returned 'next_after' "
        f"cursor each time — until done=true."
    )


# ---------------------------------------------------------------------------
# Session management (mirrors the Telegram/Discord bot integrations)
# ---------------------------------------------------------------------------

@mcp.tool(annotations=_READ_ONLY)
def dreamagent_list_sessions(project_id: int) -> str:
    """
    List a project's development sessions (conversation threads).
    Returns each session's identifier (session key) and context so you
    can select, continue, or monitor a specific development session —
    pass the session key to dreamagent_chat to continue that thread or
    to dreamagent_get_chat_status to monitor it. The session key is a
    reference used to address a session; it is not an authentication
    credential or a secret.

    Args:
        project_id: the project.
"""
    try:
        _bind_request_token()
        sessions = client().list_sessions(project_id)
    except (AuthError, DreamAgentAPIError) as e:
        return _err(e)
    except Exception:
        return "ERROR: internal error. Please retry — if it persists, check dreamagent.cloud status."

    if not sessions:
        return f"No sessions yet on project {project_id}. dreamagent_chat will create one."
    lines = []
    for s in sessions[:15]:
        state = " [archived]" if s.get("archived") else ""
        lines.append(
            f"#{s.get('id')} \"{s.get('label') or 'untitled'}\"{state} — "
            f"session_key={s.get('session_key')}"
        )
    return "\n".join(lines)


@mcp.tool(annotations=_WRITES_LOCAL)
def dreamagent_create_session(project_id: int, label: str = "ChatGPT") -> str:
    """
    Create a new development session for a project — a separate
    conversation with its own context. Use when the user wants to start
    a distinct development topic. A new session does NOT replace or
    affect the existing project — earlier sessions remain available.

    Args:
        project_id: the project.
        label: optional name for the session.
"""
    try:
        _bind_request_token()
        s = client().create_session(project_id, label=label[:60])
    except (AuthError, DreamAgentAPIError) as e:
        return _err(e)
    except Exception:
        return "ERROR: internal error. Please retry — if it persists, check dreamagent.cloud status."
    return (f"Session #{s.get('id')} \"{label}\" created — "
            f"session_key={s.get('session_key')}. Pass it to dreamagent_chat "
            f"to use this thread.")


@mcp.tool(annotations=_WRITES_LOCAL)
def dreamagent_release_project_lock(project_id: int) -> str:
    """
    RECOVERY-ONLY — releases a project's editing lock when a stale or
    abandoned session blocks new modifications. NOT a normal editing
    step, and NEVER a way to bypass an actively running edit.

    The lock state is not machine-readable from here: to judge staleness,
    check dreamagent_get_edit_progress — if no edit is active but
    dreamagent_chat still fails with a lock error, the lock is stale.
    Confirm with the user before releasing if they may have the project
    open elsewhere.

    Args:
        project_id: the project to unlock.
"""
    try:
        _bind_request_token()
        r = client().release_project_lock(project_id)
    except (AuthError, DreamAgentAPIError) as e:
        return _err(e)
    except Exception:
        return "ERROR: internal error. Please retry — if it persists, check dreamagent.cloud status."
    return f"Lock released for project {project_id}: {r}"


# ---------------------------------------------------------------------------
# Tool 5 — chat status (poll)
# ---------------------------------------------------------------------------

@mcp.tool(annotations=_READ_ONLY)
def dreamagent_get_chat_status(session_key: str, after: int = 0) -> str:
    """
    Check the progress and final status of a specific AI development
    session using its session key. Use this to monitor an asynchronous
    edit until it completes, fails, or is cancelled — keep checking
    until a terminal state. The session key comes from dreamagent_chat
    or dreamagent_list_sessions and is an identifier, not a secret.

    Returns (actual fields): 'active=', 'run_status=', 'next_after='
    (cursor for the next check), optional 'new_output:' (text produced
    since the last check), then either "Still working — poll again" or
    a terminal 'done=true' line: finished (with the final output tail),
    'the edit was NOT started: <reason>' (rejected early, e.g. HTTP 402
    insufficient credits), or 'stream error' (the connection to the run
    broke — the server-side run may still have finished; verify with
    dreamagent_get_edit_progress).
    run_status values: queued | running | cancel_requested | completed |
    failed | cancelled | interrupted | unknown.

    Args:
        session_key: the session key returned by dreamagent_chat.
        after: cursor from the previous check (0 on the first check).
"""
    try:
        _bind_request_token()
        c = client()

        status = c.chat_status(session_key)
        chunks = c.chat_chunks(session_key, after)
        local = c.local_chat_result(session_key)
    except (AuthError, DreamAgentAPIError) as e:
        return _err(e)
    except Exception:
        return "ERROR: internal error. Please retry — if it persists, check dreamagent.cloud status."

    active = bool(status.get("active")) or bool(chunks.get("active"))
    new_text = "".join(chunks.get("chunks") or [])
    next_after = chunks.get("total", after)
    run_status = chunks.get("status") or status.get("status")

    done = (not active) and (local is not None and local.get("done"))
    parts = [f"active={str(active).lower()}",
             f"run_status={run_status or 'unknown'}",
             f"next_after={next_after}"]

    if new_text:
        parts.append("new_output:\n" + new_text[-4000:])

    if done:
        err = local.get("error")
        final = (local.get("text") or "").strip()
        if err:
            if err.startswith("HTTP 4"):
                # 4xx = rejected before the run started (e.g. 402 no credits)
                parts.append(f"done=true — the edit was NOT started: {err}")
            else:
                parts.append(f"done=true (stream error: {err}; server-side run may still have finished)")
        elif final:
            parts.append("done=true. Final agent output (tail):\n" + final[-3000:])
        else:
            parts.append("done=true.")
        return "\n".join(parts)

    if not active and local is None:
        # Stream consumer ended/died but no local record — rely on chunks.
        parts.append("done=true" if not chunks.get("active") else "active=true")
        return "\n".join(parts)

    parts.append("Still working — poll again with the same session_key and next_after.")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Tool — edit progress by project (no session_key needed)
# ---------------------------------------------------------------------------

@mcp.tool(annotations=_READ_ONLY)
def dreamagent_get_edit_progress(project_id: int) -> str:
    """
    Check the latest AI edit progress for a project using its project
    identifier — no session key needed: the project id retrieves the
    session holding the latest edit. Use when the user asks whether
    their edit is finished, when the session key isn't available
    (e.g. a new conversation), and BEFORE starting any new edit.

    Returns (actual fields): 'session_key=' (the identifier of the
    session this progress comes from), 'edit_active=', 'run_status=',
    'chunks=' (output size so far), optional 'recent_output (tail):',
    and a terminal 'result:' line: finished (with the final output
    tail), 'the edit was NOT started: <reason>' (rejected early,
    e.g. HTTP 402 insufficient credits), 'stream error', or 'no run is
    currently active'.
    run_status values: queued | running | cancel_requested | completed |
    failed | cancelled | interrupted | unknown.

    Distinct from dreamagent_get_project_status: project status =
    creation/deployment state (creating/ready/failed); edit progress =
    current AI modification state; chat status (dreamagent_get_chat_status)
    monitors one specific session by its session key. If edit_active is
    true, do NOT launch another edit for the same project.

    Args:
        project_id: the project being edited.
"""
    try:
        _bind_request_token()
        c = client()
        session_key, how = c.resolve_progress_session(project_id)
        if not session_key:
            return (f"No chat sessions found on project {project_id} — no edits "
                    f"have been made yet. Use dreamagent_chat to send one.")
        status = c.chat_status(session_key)
        chunks = c.chat_chunks(session_key, 0)
        local = c.local_chat_result(session_key)
    except (AuthError, DreamAgentAPIError) as e:
        return _err(e)
    except Exception:
        return "ERROR: internal error. Please retry — if it persists, check dreamagent.cloud status."

    active = bool(status.get("active")) or bool(chunks.get("active"))
    run_status = chunks.get("status") or status.get("status")
    total = chunks.get("total", 0)
    all_text = "".join(chunks.get("chunks") or [])

    parts = [f"session_key={session_key} ({how})",
             f"edit_active={str(active).lower()}",
             f"run_status={run_status or 'unknown'}",
             f"chunks={total}"]

    if all_text.strip():
        parts.append("recent_output (tail):\n" + all_text[-1500:])

    if not active:
        if local is not None and local.get("done"):
            err = local.get("error")
            final = (local.get("text") or "").strip()
            if err and err.startswith("HTTP 4"):
                parts.append("result: the edit was NOT started: " + err)
            elif err:
                parts.append("result: stream error (server-side run may still have finished): " + err)
            elif final:
                parts.append("result: finished. Final output (tail):\n" + final[-2000:])
            else:
                parts.append("result: finished.")
        else:
            parts.append("result: no run is currently active.")
    else:
        parts.append(
            f"The edit is still running — poll dreamagent_get_chat_status("
            f"session_key=\"{session_key}\", after={total}) for incremental output."
        )
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Tool 6 — cancel chat (safety valve)
# ---------------------------------------------------------------------------

@mcp.tool(annotations=_DESTRUCTIVE)
def dreamagent_cancel_chat(session_key: str) -> str:
    """
    DESTRUCTIVE — stops an AI edit that is currently running. Use ONLY
    when the user explicitly asks to stop/cancel the active edit.
    After cancellation, do NOT claim the requested change was
    completed — report that it was stopped.

    Returns 'Cancellation requested: <backend message>' — e.g. 'Query
    cancelled', 'Cancellation requested' (durable run), or 'No active
    query found'. Afterwards poll dreamagent_get_chat_status /
    dreamagent_get_edit_progress until run_status reports 'cancelled'.

    Args:
        session_key: the session key of the running edit.
"""
    try:
        _bind_request_token()
        r = client().cancel_chat(session_key)
    except (AuthError, DreamAgentAPIError) as e:
        return _err(e)
    except Exception:
        return "ERROR: internal error. Please retry — if it persists, check dreamagent.cloud status."
    return f"Cancellation requested: {r.get('message', 'ok')}"


# ---------------------------------------------------------------------------
# Website screenshot — WEBSITE projects. Rendered by headless chromium on
# THIS host (the server runs on the main VPS), saved under screenshots/ and
# served back at /screenshots/<uuid>.png (unguessable names).
# ---------------------------------------------------------------------------

SCREENSHOT_DIR = os.getenv("SCREENSHOT_DIR") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "screenshots")
SCREENSHOT_PUBLIC_BASE = os.getenv("SCREENSHOT_PUBLIC_BASE", "https://mcp.dreamagent.cloud").rstrip("/")
# Storage hygiene: screenshots are ephemeral artifacts (returned inline to
# the caller AND served at unguessable URLs). The sweep below deletes them
# after SCREENSHOT_TTL_HOURS (default 24) and keeps the directory under
# SCREENSHOT_MAX_MB (default 200) by removing oldest first.
SCREENSHOT_TTL_HOURS = max(1, int(os.getenv("SCREENSHOT_TTL_HOURS", "24")))
SCREENSHOT_MAX_MB = max(10, int(os.getenv("SCREENSHOT_MAX_MB", "200")))


def _sweep_screenshots() -> str:
    """Delete expired screenshots (TTL) and enforce the directory size cap
    (oldest first). Returns a short summary line; never raises."""
    import time
    try:
        if not os.path.isdir(SCREENSHOT_DIR):
            return ""
        now = time.time()
        ttl_s = SCREENSHOT_TTL_HOURS * 3600
        max_bytes = SCREENSHOT_MAX_MB * 1024 * 1024
        entries = []
        for name in os.listdir(SCREENSHOT_DIR):
            if not re.fullmatch(r"[0-9a-f]{32}\.png", name):
                continue  # never touch anything that isn't ours
            path = os.path.join(SCREENSHOT_DIR, name)
            try:
                st = os.stat(path)
                entries.append((path, st.st_mtime, st.st_size))
            except OSError:
                continue
        removed = freed = 0
        kept = []
        for path, mtime, size in entries:
            if now - mtime > ttl_s:
                try:
                    os.unlink(path)
                    removed += 1
                    freed += size
                except OSError:
                    pass
            else:
                kept.append((path, mtime, size))
        kept.sort(key=lambda e: e[1])  # oldest first
        total = sum(s for _, _, s in kept)
        for path, mtime, size in kept:
            if total <= max_bytes:
                break
            try:
                os.unlink(path)
                removed += 1
                freed += size
                total -= size
            except OSError:
                pass
        if removed:
            logger.info("screenshot sweep: removed %d file(s), freed %.1f MB, dir now %.1f MB",
                        removed, freed / 1e6, total / 1e6)
            return (f" Cleanup: removed {removed} expired screenshot(s), "
                    f"freed {freed / 1e6:.1f} MB.")
        return ""
    except Exception:
        return ""


def _public_http_url_or_raise(raw: str) -> str:
    """SSRF guard: absolute http(s) URL resolving to PUBLIC space only.

    Returns the validated hostname + its resolved public IPs so the caller
    can pin them into the browser (DNS-rebinding protection: the check and
    the fetch must use the SAME answer).
    """
    import ipaddress
    import socket
    from urllib.parse import urlparse

    u = urlparse(raw or "")
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError("url must be an absolute http(s) URL")
    host = u.hostname.strip().rstrip(".").lower()
    if host == "localhost" or host.endswith((".local", ".internal")):
        raise ValueError("local hostnames are not allowed")
    if len(raw) > 2000:
        raise ValueError("url too long")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise ValueError(f"cannot resolve host: {host}")
    ips = []
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast):
            raise ValueError("private/internal targets are not allowed")
        if str(ip) not in ips:
            ips.append(str(ip))
    return host, ips


def _chromium_binary() -> str:
    import shutil
    for b in ("/usr/bin/chromium", "/usr/bin/chromium-browser",
              "/usr/bin/google-chrome"):
        if os.path.isfile(b):
            return b
    found = shutil.which("chromium") or shutil.which("chromium-browser") \
        or shutil.which("google-chrome")
    if not found:
        raise RuntimeError("no headless chromium/chrome binary on this host")
    return found


@mcp.tool(annotations=_READ_ONLY)
def dreamagent_screenshot_website(url: str, width: int = 1280, height: int = 800):
    """WEBSITE PROJECTS ONLY — capture a screenshot of any public web page by URL.

    Renders the page with DreamAgent's server-side headless browser and
    returns the PNG as an MCP image content block (for direct visual
    inspection) PLUS a public URL you can view, embed, or compare. Use it
    to: check a website project's live pages after edits or deploys, review
    a layout for UX issues, capture a design reference the user points at,
    or verify a page renders correctly.

    No project id needed — just the URL. Only public http(s) pages;
    private/internal hosts are rejected.

    Args:
        url: absolute http(s) URL of the page to capture
        width: viewport width 320-1920 (default 1280)
        height: viewport height 240-2160 (default 800)

    Returns:
        [text] capture info + public PNG URL
        (https://mcp.dreamagent.cloud/screenshots/...), [image] the PNG
        itself as base64 — vision-capable clients can inspect it directly.
        Viewports whose PNG exceeds ~5 MB return the URL only (use a
        smaller viewport for inline review).

    Errors:
        'ERROR: ...' for a bad/unreachable URL, a private target, a render
        timeout (45 s), or no chromium on the host. Pages that require login
        or block bots may render blank or partially — try again or accept
        the partial capture.
    """
    import subprocess
    import tempfile
    import uuid

    try:
        host, ips = _public_http_url_or_raise(url.strip())
    except ValueError as e:
        return f"ERROR: {e}"

    width = max(320, min(1920, int(width)))
    height = max(240, min(2160, int(height)))

    out = os.path.join(tempfile.gettempdir(), f"snap_{uuid.uuid4().hex}.png")
    # Pin the validated IPs into chromium: the SSRF check above resolved the
    # host, and this makes the BROWSER use exactly those answers — a DNS
    # rebinding attack (public IP at check time, internal IP at fetch time)
    # cannot redirect the render at an internal service.
    resolver_rules = ",".join(f"MAP {host} {ip}" for ip in ips)
    cmd = [
        _chromium_binary(), "--headless=new", "--disable-gpu", "--no-sandbox",
        "--hide-scrollbars", "--disable-dev-shm-usage",
        f"--host-resolver-rules={resolver_rules}",
        f"--window-size={width},{height}",
        "--virtual-time-budget=10000",  # let JS/layout settle before the shot
        f"--screenshot={out}", url.strip(),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=45)
    except Exception:
        return "ERROR: screenshot timed out (45 s) — the page may be too heavy; retry once."
    try:
        if not os.path.isfile(out) or os.path.getsize(out) == 0:
            detail = (proc.stderr or proc.stdout or b"").decode("utf-8", "replace")[-250:]
            return f"ERROR: browser produced no image. {detail}"
        data = open(out, "rb").read()
    finally:
        try:
            os.unlink(out)
        except OSError:
            pass
    if len(data) > 15 * 1024 * 1024:
        return "ERROR: screenshot too large (>15 MB) — reduce the viewport."

    os.makedirs(SCREENSHOT_DIR, exist_ok=True)
    name = f"{uuid.uuid4().hex}.png"
    with open(os.path.join(SCREENSHOT_DIR, name), "wb") as fh:
        fh.write(data)
    logger.info("screenshot %s -> %s (%d bytes)", url, name, len(data))
    sweep_note = _sweep_screenshots()

    public_url = f"{SCREENSHOT_PUBLIC_BASE}/screenshots/{name}"
    text = (f"Screenshot saved ({width}x{height}, {len(data)} bytes).\n"
            f"URL: {public_url}\n"
            f"File: {os.path.join(SCREENSHOT_DIR, name)}")
    if sweep_note:
        text += "\n" + sweep_note.strip()

    # Inline image block for vision-capable clients. Cap at ~5 MB encoded
    # (OpenAI-style image input limits); bigger captures stay URL-only.
    b64 = base64.b64encode(data).decode("ascii")
    if len(b64) > 5 * 1024 * 1024:
        text += ("\nNOTE: PNG too large for inline image content — "
                 "use the URL above or retry with a smaller viewport.")
        return text
    return [TextContent(type="text", text=text),
            ImageContent(type="image", data=b64, mimeType="image/png")]


@mcp.custom_route("/screenshots/{name}", methods=["GET"])
async def screenshot_endpoint(request):
    from starlette.responses import FileResponse, JSONResponse
    import re
    name = request.path_params.get("name", "")
    if not re.fullmatch(r"[0-9a-f]{32}\.png", name):
        return JSONResponse({"error": "not found"}, status_code=404)
    path = os.path.join(SCREENSHOT_DIR, name)
    if not os.path.isfile(path):
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(path, media_type="image/png")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="DreamAgent MCP server")
    parser.add_argument("--http", action="store_true",
                        help="serve over streamable HTTP instead of stdio (for remote MCP)")
    parser.add_argument("--host", default=os.getenv("MCP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("MCP_PORT", "8800")))
    args = parser.parse_args()

    # Housekeeping: expire old screenshots even if no new captures come in.
    _sweep_screenshots()

    # Local mode: env credentials exist -> verify them once at startup.
    # Hosted mode (no env credentials): every request authenticates with the
    # caller's DreamAgent API key from the Authorization header.
    if os.getenv("DREAMAGENT_EMAIL") and os.getenv("DREAMAGENT_PASSWORD"):
        try:
            client().auth.get_token()
            logger.info("DreamAgent account connected (user: %s)",
                        client().auth.user.get("email"))
        except AuthError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2
    else:
        logger.info("Hosted mode: authenticating per-request from the "
                    "Authorization header (DreamAgent API keys)")

    if args.http:
        mcp.run(transport="http", host=args.host, port=args.port)
    else:
        mcp.run()  # stdio
    return 0


if __name__ == "__main__":
    sys.exit(main())
