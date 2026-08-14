# DreamAgent MCP Server

Standalone MCP server that lets **ChatGPT operate your DreamAgent account**:
create projects (Telegram bots, Discord bots, websites, schedulers), watch them
build, and edit them conversationally through DreamAgent's AI agent.

It talks **only to the existing DreamAgent REST API** — no backend changes, and
all server-side ownership checks apply unchanged.

## Tools

| Tool | What it does |
|---|---|
| `dreamagent_list_projects` | List your projects (id, type, status, domain) |
| `dreamagent_create_project` | Create a telegrambot / discordbot / website / scheduler (async) |
| `dreamagent_get_project_status` | Poll a project until `ready` / `failed` |
| `dreamagent_chat` | Send a build/edit/debug instruction to the project's AI agent (async, consumes credits) |
| `dreamagent_get_chat_status` | Poll the agent run; streams new output + `next_after` cursor |
| `dreamagent_cancel_chat` | Cancel a running edit |

Long operations are **submit-then-poll**: create → poll status; chat → poll
chat status. The tool descriptions tell ChatGPT to do this.

## Setup

```bash
cd mcp-server
pip install -r requirements.txt
cp .env.example .env       # fill in your DreamAgent email + password
```

The account is connected by logging into the **existing** `POST /auth/login`
endpoint; the session token is cached in memory and refreshed on expiry.

## Run

```bash
# stdio (local clients: Claude Desktop, Claude Code, etc.)
python server.py

# HTTP (remote MCP — for ChatGPT apps)
python server.py --http --port 8800
# then expose https://your-host:8800/mcp via your reverse proxy
```

## Connect to ChatGPT (developer apps / MCP)

1. Run the server with `--http` behind HTTPS (e.g. `https://mcp.dreamagent.cloud/mcp`).
2. In your ChatGPT app config (or any MCP client that supports remote servers),
   point the MCP endpoint at that URL.
3. Per-user credentials: set `DREAMAGENT_EMAIL` / `DREAMAGENT_PASSWORD` for the
   account the server should act as. (For a multi-user deployment, run one
   instance per user or wait for the OAuth phase.)

## Try it locally (smoke test)

```bash
python - <<'PY'
from server import mcp
tools = mcp._tool_manager.list_tools() if hasattr(mcp, "_tool_manager") else []
print([t.name for t in tools])
PY
# or simply:
fastmcp dev server.py
```

## Example ChatGPT conversations

- "Show my DreamAgent projects"
- "Create a Telegram bot called WeatherBot, token 123456:ABC — it should reply
  with the weather when I send a city name" → create → poll until ready
- "Add a /price command to my Discord bot" → dreamagent_chat → poll until done
- "Is my website finished building?"
- "Cancel the edit running on my crypto bot"

## Security notes

- Credentials live only in the `.env` you control; the session token is
  transient and refreshed via the existing login endpoint.
- Every API call carries the user's own token — DreamAgent's per-user
  authorization and rate limits apply exactly as in the dashboard.
- Bot tokens passed to `dreamagent_create_project` are sent to DreamAgent over
  HTTPS and stored in the project's `.env`, the same as creating via the
  dashboard.
