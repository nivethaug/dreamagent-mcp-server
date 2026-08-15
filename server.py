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
        "HOW DREAMAGENT WORKS — you only describe the CHANGE:\n"
        "DreamAgent's agent knows its own tech stack, reads its own logs and "
        "code index first, edits the code, runs the tests, rebuilds, redeploys "
        "to production, and auto-commits to git — all automatically after "
        "receiving a message. So the edit message should contain ONLY the "
        "desired change (what the user wants, plus brief behavioral "
        "constraints if relevant). NEVER include process or ops instructions "
        "such as 'run the build/tests and deploy', 'check the logs', "
        "'fix build issues', tech-stack guidance, file paths, or "
        "implementation steps — those are DreamAgent's job and only add noise.\n"
        "GOOD: 'Replace the dashboard cards with a cleaner 2-column layout; "
        "keep the existing color palette.'\n"
        "BAD: '...after implementing, run the normal build/tests, fix any "
        "issues, and deploy the updated project.'\n\n"
        "WORKFLOW: long ops are submit-then-poll — after creating a project "
        "or sending an edit, POLL the matching status tool until it reports "
        "ready/completed, then summarize the result for the user.\n"
        "Bot tokens: when the user wants a Telegram/Discord bot, ask them to "
        "paste the bot token in the conversation and pass it to the create "
        "tool — this is the standard DreamAgent flow (sent over HTTPS, stored "
        "as a server-side project secret)."
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
    """List the user's DreamAgent projects.

    Returns id, name, type, status and domain for each project. Use this to
    find the project_id before editing, and to see which projects are ready
    or failed.

    Args:
        status: optional filter — one of creating/ready/failed.
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
def dreamagent_create_project(
    name: str,
    project_type: str,
    bot_token: str | None = None,
    description: str | None = None,
    env_vars: dict | None = None,
) -> str:
    """Create a new DreamAgent project. Creation is ASYNC — call
    dreamagent_get_project_status afterwards and poll until status is
    'ready' (bots take ~2-5 minutes; websites longer).

    Types: telegrambot, discordbot, website, scheduler (or tradingbot).
    A Telegram/Discord bot token from @BotFather / the Discord developer
    portal is REQUIRED for telegrambot and discordbot.

    BEFORE CALLING — act as DreamAgent's Prompt Builder (Creative Director):
    - Infer before asking; make tasteful assumptions instead of long
      questionnaires. Ask only 1-3 high-value follow-ups (purpose,
      audience, style, key features) — max two rounds, then decide.
    - For broad ideas, offer 3-5 curated creative directions with short
      evocative names, not generic categories.
    - Only call this tool once the idea is clear enough to build.

    THE DESCRIPTION = the final creation prompt. Follow DreamAgent's
    prompt style:
    - Describe WHAT to build — product vision, experience, users, features,
      tone and design direction — not how to engineer it.
    - DreamAgent Project AI already knows its tech stack, structure, and
      deployment pipeline. NEVER include tech-stack, architecture, auth,
      API, database, deployment, CI/CD, or testing details.
    - Creative projects: open with Experience Vision → Hero Scene → Visitor
      Journey → Visual Identity, then features. Elevate imaginative ideas —
      never reduce them to generic informational pages.
    - Bots: purpose, target users, exact commands with example behavior,
      personality/tone, data sources or APIs to use.
    - Business/internal tools: professional, practical direction — clean
      dashboards only when a dashboard is actually wanted.
    - Concise markdown, compact bullets, depth scaled to complexity; end
      with a clear final-result expectation.
    GOOD description (bot): 'A friendly customer-support assistant for a
    small coffee shop. Commands: /menu (browse drinks with prices),
    /order <item> (start an order flow), /faq (common questions), /agent
    (request a human). Warm, slightly playful tone. Store FAQs in an
    editable knowledge file the owner can update without code.'
    BAD description: 'A bot with Node.js backend, PostgreSQL, REST API,
    Docker deployment, error handling and tests.'

    IMPORTANT — bot token handling: the normal, expected flow is that the
    user pastes the bot token directly into the conversation and you pass
    it to this tool as bot_token. This is identical to DreamAgent's own
    dashboard create-dialog: the token travels over HTTPS, is stored as a
    server-side project secret (.env), and is never logged. Do NOT refuse
    the token, do NOT tell the user to pre-configure environment variables,
    and do NOT invent alternate secret-handling steps — just ask for the
    token if the user wants a bot and hasn't provided it yet.

    Args:
        name: project name (max 30 chars; a public subdomain is auto-generated).
        project_type: one of telegrambot/discordbot/website/scheduler.
        bot_token: the Telegram bot token (telegrambot) or Discord bot token
            (discordbot). Required for those two types — ask the user to paste it.
        description: the refined creation prompt (see style above).
        env_vars: optional dict of extra environment variables for the project.
    """
    try:
        _bind_request_token()
        p = client().create_project(name, project_type, bot_token, description, env_vars)
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
    """Get a project's current status. Poll this after creation and after
    AI edits. Statuses: creating (still building), ready (live), failed.

    Args:
        project_id: numeric project id (from dreamagent_list_projects / create).
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
    """Send a build/edit/debug instruction to the project's DreamAgent AI
    agent. The agent edits the code, rebuilds and redeploys the project, and
    auto-commits to git — automatically. Example messages: 'add a /weather
    command that replies with the weather for a city', 'fix the login bug',
    'change the site theme to dark'.

    MESSAGE CONTENT: describe ONLY the desired change. The agent already
    knows its tech stack and ALWAYS reads logs, tests, rebuilds and deploys
    on its own — do NOT add instructions like 'run the build/tests and
    deploy' or 'fix any build issues'; they are redundant noise.

    SESSIONS: by default this uses the project's dedicated 'ChatGPT' session
    (created on first use, full conversation history kept). Pass
    session_key (from dreamagent_list_sessions) to continue a specific
    session, or new_session=true to start a fresh one.

    This is ASYNC and consumes AI credits. Immediate rejections (no credits,
    session locked, edit already running) are returned as errors right away;
    otherwise poll dreamagent_get_chat_status(session_key) until done=true
    (typically 1-10 minutes).

    Args:
        project_id: the project to edit.
        message: natural-language instruction for the agent.
        session_key: optional — continue this specific session instead of the
            default 'ChatGPT' one.
        new_session: optional — start a brand-new session for this edit.
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
    """List the chat sessions of a project (conversation threads with the
    project's AI agent). Use session_keys with dreamagent_chat to continue a
    specific thread.

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
    """Create a new chat session (conversation thread) on a project. Use when
    starting a distinct topic so it doesn't mix with earlier history.

    Args:
        project_id: the project.
        label: optional name for the session (default 'ChatGPT').
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
    """Force-release a project's chat lock (last resort when edits fail with
    'session locked' and the locking session is stale/closed). WARNING: if
    the user has the project chat open in the DreamAgent dashboard, this
    interrupts that session — confirm with the user first.

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
    """Poll the progress of a dreamagent_chat run. Returns whether the run
    is still active, any new text produced since the 'after' cursor, and the
    next cursor to pass on the following call. Keep polling until done=true.

    Args:
        session_key: the session_key returned by dreamagent_chat.
        after: the 'next_after' cursor returned by the previous poll (0 first time).
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
    """Check the progress of the latest AI edit on a project WITHOUT needing
    a session_key — use this when you don't have one from a previous
    dreamagent_chat call (e.g. a new conversation, or the user just asks
    'is my edit done?'). Returns whether an edit is running, its recent
    output, and the session_key to use for tighter follow-up polling with
    dreamagent_get_chat_status.

    Note: dreamagent_get_project_status is about CREATION/deployment state
    and stays 'ready' during edits — it does NOT reflect edit progress.
    Use THIS tool (or dreamagent_get_chat_status) for edits.

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
    """Cancel a running dreamagent_chat edit for the given session_key.

    Args:
        session_key: the session_key returned by dreamagent_chat.
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
