#!/usr/bin/env python3
"""
DreamAgent MCP Server — standalone.

Lets ChatGPT (or any MCP client) operate the user's DreamAgent account:
create projects (Telegram/Discord bots, websites, schedulers), check their
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
import logging
import os
import sys

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers

from auth import AuthManager, AuthError
from dreamagent_client import DreamAgentClient, DreamAgentAPIError, PROJECT_TYPES
from dreamagent_client import set_request_token, get_request_token
import oauth as oauth_as

register_oauth = oauth_as.register_oauth_routes

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("dreamagent.mcp")

mcp = FastMCP(
    "DreamAgent",
    instructions=(
        "Operate the user's DreamAgent account: build and deploy apps and bots "
        "(websites, Telegram bots, Discord bots, schedulers) and edit them via "
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
        "2. NEW PROJECT — create_project → get_project_status (poll until "
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
            "Settings → Connect to ChatGPT)."
        )
    return f"ERROR: {e}"


# ---------------------------------------------------------------------------
# Tool 1 — list projects
# ---------------------------------------------------------------------------

@mcp.tool()
def dreamagent_list_projects(status: str | None = None) -> str:
    """
    List the user's DreamAgent projects.

    Returns each project's name, ID, type, status, and live domain (when
    available). Use when the user asks to see, find, select, or check their
    projects — and ALWAYS before modifying a project when its ID is not
    already known (resolve names to IDs here first).

    Args:
        status: optional filter — creating/ready/failed.
"""
    try:
        _bind_request_token()
        projects = client().list_projects(status)
    except (AuthError, DreamAgentAPIError) as e:
        return _err(e)

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

@mcp.tool()
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

    if not items:
        return ("No saved credentials yet. The user must add them at "
                "dreamagent.cloud → Settings → Global Integrations "
                "(one-time, verified, reusable). Tokens are never accepted "
                "in chat — wait for them to save it, then call this tool again.")
    lines = []
    for gi in items:
        v = " ✓verified" if gi.get("verified") else ""
        lines.append(
            f"#{gi.get('id')} [{gi.get('token_type')}] {gi.get('key_name')} — "
            f"{gi.get('title') or 'no title'}{v}"
        )
    return ("\n".join(lines)
            + "\nUse these ids in dreamagent_create_project instead of asking "
              "the user to paste tokens.")


@mcp.tool()
def dreamagent_list_project_env(project_id: int) -> str:
    """
    List a project's environment variables with their details.

    Shows each key's name, title, description, docs URL, and category —
    values are masked (secrets are never returned). Use to see which
    integrations a project already has before adding more, or to answer
    "what keys does my project have?".

    These are PROJECT-SPECIFIC variables — distinct from the user's
    Global Integrations (dreamagent_list_global_integrations).

    Args:
        project_id: the project to inspect.
"""
    try:
        _bind_request_token()
        data = client().list_project_env(project_id)
    except (AuthError, DreamAgentAPIError) as e:
        return _err(e)

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


@mcp.tool()
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
    bot, Discord bot, or scheduler). Use ONLY when the user clearly asks
    to create/build a new project; not for questions about what to build.

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

    DESCRIPTION should be the refined build request: what to build, for
    whom, features/commands, tone and design direction. No tech-stack,
    architecture, or deployment details — DreamAgent handles those.

    Args:
        name: project name (max 30 chars; a public subdomain is auto-generated).
        project_type: website / telegrambot / discordbot / scheduler.
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

    return (
        f"Project #{p.get('id')} \"{p.get('name')}\" ({project_type}) queued for creation. "
        f"Status: {p.get('status')}. Domain will be: {p.get('domain')}. "
        f"Poll dreamagent_get_project_status(project_id={p.get('id')}) until status is 'ready'."
    )


# ---------------------------------------------------------------------------
# Tool 3 — project status
# ---------------------------------------------------------------------------

@mcp.tool()
def dreamagent_get_project_status(project_id: int) -> str:
    """
    Check a project's creation/deployment state: creating, ready, or
    failed. Use after dreamagent_create_project (poll until ready) and
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

@mcp.tool()
def dreamagent_chat(project_id: int, message: str,
                    session_key: str | None = None, new_session: bool = False) -> str:
    """
    WRITE ACTION — builds, modifies, or fixes an EXISTING project with
    a natural-language instruction. Use ONLY when the user clearly asks
    for a change ("add X", "fix Y", "change the theme"). If the user is
    only asking for advice, analysis, or suggestions ("what could be
    improved?"), do NOT call this — answer from the project's info
    instead.

    Describe ONLY the desired change — DreamAgent's AI automatically
    handles code, tests, rebuild, and redeployment. Completed changes
    are saved and committed automatically.

    ASYNCHRONOUS (states: queued → running → completed | failed |
    cancelled): returns immediately; monitor with
    dreamagent_get_edit_progress (or dreamagent_get_chat_status with the
    returned session key) until a terminal state. Report success only
    after 'completed'. Edits consume the user's AI credits; on
    insufficient credits the call fails immediately with a clear error.

    ONE EDIT AT A TIME: never start another modification on the same
    project while one is running — check progress first and wait.

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

    return (
        f"Edit submitted to project {project_id} (session_key={session_key}). "
        f"The agent is now working. Poll dreamagent_get_chat_status("
        f"session_key=\"{session_key}\") — pass back the returned 'next_after' "
        f"cursor each time — until done=true."
    )


# ---------------------------------------------------------------------------
# Session management (mirrors the Telegram/Discord bot integrations)
# ---------------------------------------------------------------------------

@mcp.tool()
def dreamagent_list_sessions(project_id: int) -> str:
    """
    List a project's development sessions (conversation threads).
    Each session preserves context for related work on that project —
    continuing one keeps the conversation and project context. Use to
    identify or resume a specific thread.

    Args:
        project_id: the project.
"""
    try:
        _bind_request_token()
        sessions = client().list_sessions(project_id)
    except (AuthError, DreamAgentAPIError) as e:
        return _err(e)

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


@mcp.tool()
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
    return (f"Session #{s.get('id')} \"{label}\" created — "
            f"session_key={s.get('session_key')}. Pass it to dreamagent_chat "
            f"to use this thread.")


@mcp.tool()
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
    return f"Lock released for project {project_id}: {r}"


# ---------------------------------------------------------------------------
# Tool 5 — chat status (poll)
# ---------------------------------------------------------------------------

@mcp.tool()
def dreamagent_get_chat_status(session_key: str, after: int = 0) -> str:
    """
    Check the progress of a specific AI edit, by session. Use after
    dreamagent_chat when you have the session key — keep checking until
    a terminal state.

    Returns (real fields): edit_active (true while queued/running),
    run_status (queued | running | completed | failed | cancelled |
    unknown), next_after (cursor for the next check), new_output (text
    produced since the last check), and on completion result: finished
    (with final output) / NOT started (rejected, e.g. insufficient
    credits) / stream error.

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

@mcp.tool()
def dreamagent_get_edit_progress(project_id: int) -> str:
    """
    Check the latest AI edit progress for a project — no session key
    needed. Use when the user asks whether their edit is finished, when
    the session key isn't available (e.g. a new conversation), and
    BEFORE starting any new edit.

    Returns (real fields): session_key, edit_active, run_status
    (queued | running | completed | failed | cancelled | unknown),
    recent_output, and result on completion.

    Distinct from dreamagent_get_project_status: project status =
    creation/deployment state (creating/ready/failed); edit progress =
    current AI modification state. If edit_active is true, do NOT
    launch another edit for the same project.

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

@mcp.tool()
def dreamagent_cancel_chat(session_key: str) -> str:
    """
    DESTRUCTIVE — stops an AI edit that is currently running. Use ONLY
    when the user explicitly asks to stop/cancel the active edit.
    After cancellation, do NOT claim the requested change was
    completed — report that it was stopped.

    Args:
        session_key: the session key of the running edit.
"""
    try:
        _bind_request_token()
        r = client().cancel_chat(session_key)
    except (AuthError, DreamAgentAPIError) as e:
        return _err(e)
    return f"Cancellation requested: {r.get('message', 'ok')}"


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
