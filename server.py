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

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("dreamagent.mcp")

mcp = FastMCP(
    "DreamAgent",
    instructions=(
        "Operate the user's DreamAgent account: build and deploy apps and bots "
        "(websites, Telegram bots, Discord bots, schedulers) and edit them via "
        "DreamAgent's AI agent. Long operations are submit-then-poll — after "
        "creating a project or sending an edit, POLL the matching status tool "
        "until it reports ready/completed."
    ),
)

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
    """Bind the incoming MCP request's Authorization header (hosted mode).

    Called at the top of every tool. If the caller sent no Bearer token and
    no env credentials exist, tools raise a friendly connect-your-account
    error on first API use.
    """
    try:
        headers = get_http_headers() or {}
    except Exception:
        headers = {}
    auth_header = headers.get("authorization") or headers.get("Authorization") or ""
    token = None
    parts = auth_header.split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        token = parts[1]
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

    Args:
        name: project name (max 30 chars; a public subdomain is auto-generated).
        project_type: one of telegrambot/discordbot/website/scheduler.
        bot_token: the Telegram bot token (telegrambot) or Discord bot token
            (discordbot). Required for those two types.
        description: optional — what the bot/site should do; guides the AI build.
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
def dreamagent_chat(project_id: int, message: str) -> str:
    """Send a build/edit/debug instruction to the project's DreamAgent AI
    agent. The agent edits the code, rebuilds and redeploys the project, and
    auto-commits to git. Example messages: 'add a /weather command that
    replies with the weather for a city', 'fix the login bug', 'change the
    site theme to dark'.

    This is ASYNC and consumes AI credits: it returns immediately with a
    session_key — then poll dreamagent_get_chat_status(session_key) until
    the run completes (typically 1-10 minutes).

    Args:
        project_id: the project to edit.
        message: natural-language instruction for the agent.
    """
    try:
        _bind_request_token()
        c = client()

        session = c.ensure_session(project_id)
        session_key = session["session_key"]
        c.submit_chat(session_key, message)
    except (AuthError, DreamAgentAPIError) as e:
        return _err(e)

    return (
        f"Edit submitted to project {project_id} (session_key={session_key}). "
        f"The agent is now working. Poll dreamagent_get_chat_status("
        f"session_key=\"{session_key}\") — pass back the returned 'next_after' "
        f"cursor each time — until done=true."
    )


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
