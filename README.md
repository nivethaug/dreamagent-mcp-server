<div align="center">

# DreamAgent MCP

**Turn natural-language requests into deployed applications.**

Connect ChatGPT, Claude, or any MCP client to [DreamAgent](https://dreamagent.cloud) —
build websites, Telegram bots, Discord bots and schedulers from a conversation,
watch them go live, then keep iterating.

`streamable-http` · OAuth 2.1 + PKCE · 10 tools · Python / [FastMCP](https://gofastmcp.com)

</div>

---

## What DreamAgent does

[DreamAgent](https://dreamagent.cloud) is an AI app-builder platform: you describe
what you want, its agent writes the code, runs the tests, rebuilds, redeploys to
production and commits to git — automatically. This MCP server exposes that
power to your AI chat, so:

> **You:** "Create a Telegram bot called WeatherBot — token `123:ABC` — it replies
> with the weather when I send a city name."
>
> **ChatGPT:** creates it → polls → *"WeatherBot is live."*
>
> **You:** "Add a /forecast command for 5-day forecasts."
>
> **ChatGPT:** sends the edit → the agent codes, tests, redeploys → done.

## Tools

| Tool | What it does |
|---|---|
| `dreamagent_list_projects` | List your projects (id, type, status, domain) |
| `dreamagent_create_project` | Create a website / Telegram bot / Discord bot / scheduler |
| `dreamagent_get_project_status` | Poll a build until `ready` / `failed` |
| `dreamagent_chat` | Send a build/edit/debug instruction to the project's AI agent |
| `dreamagent_get_chat_status` | Poll an edit run (incremental output) |
| `dreamagent_get_edit_progress` | Edit progress by project — no session key needed |
| `dreamagent_list_sessions` | List conversation threads per project |
| `dreamagent_create_session` | Start a fresh topic thread |
| `dreamagent_cancel_chat` | Cancel a running edit |
| `dreamagent_release_project_lock` | Last-resort unlock (confirm first) |

Long operations are **submit-then-poll** — create → poll status, edit → poll
progress. The agent handles code, tests, rebuild, redeploy and git commits by
itself; prompts only describe the *change*.

## Connect — ChatGPT

**With the official app (recommended):** find *DreamAgent* in the ChatGPT app
directory → **Connect** → sign in with your DreamAgent account (email or
Google).

**As a custom connector:**

1. In DreamAgent → **Settings → Connect to ChatGPT** → **Create Key** → copy
   the full URL (it contains your key)
2. ChatGPT → **Settings → Apps & Connectors → Advanced** → **Add custom connector**
3. Paste the URL as the **MCP Server URL**, choose **OAuth** (or *No
   authentication* — the key rides in the URL), save
4. Ask: *"Show my DreamAgent projects"*

## Connect — Claude

Claude → **Settings → Connectors → Add custom connector**:

- **Name:** `DreamAgent`
- **Remote MCP server URL:** `https://mcp.dreamagent.cloud/mcp`
- **OAuth:** leave empty — Claude auto-discovers it

Click **Connect**, sign in with DreamAgent, done.

## Connect — any MCP client

```json
{
  "mcpServers": {
    "dreamagent": {
      "type": "streamable_http",
      "url": "https://mcp.dreamagent.cloud/mcp"
    }
  }
}
```

Auth is OAuth 2.1 (authorization-code + PKCE, RFC 7591 dynamic registration)
— or pass a DreamAgent API key as `Authorization: Bearer da_...` /
`?key=da_...`.

## Example prompts

- *"Show my DreamAgent projects"*
- *"Create a Discord bot called PriceBot — token `<from Discord dev portal>` —
  it shows crypto prices with /price and /top commands"*
- *"Is my website finished building?"*
- *"My crypto bot stopped replying — what's wrong?"* (edits start with the
  agent reading the project logs)
- *"Add a dark-mode toggle to my documentation site"*
- *"Roll back yesterday's dashboard change"* *(via chat)*
- *"How many AI credits do I have left?"*

## How it works

```
ChatGPT / Claude / Glama
   │  MCP (streamable HTTP) + OAuth 2.1
   ▼
mcp.dreamagent.cloud ── this server (FastMCP)
   │  per-request Bearer = the user's own DreamAgent API key
   ▼
api.dreamagent.cloud ── DreamAgent platform
   └── agent writes code → tests → rebuild → deploy → git commit
```

Every request runs as the connected user — DreamAgent's ownership checks,
plan limits and AI-credit billing apply exactly as in the dashboard.

## Security & privacy

- **Your key, your account.** OAuth issues a revocable DreamAgent API key
  (`da_...`) at connect time — visible and revocable any time in
  *Settings → Connect to ChatGPT*. Revoking disconnects that client instantly;
  other clients keep working.
- **OAuth done right.** Authorization-code + **PKCE (S256) mandatory**,
  single-use 120-second codes, redirect-URI allowlists (loopback per RFC 8252),
  login rate-limiting, secrets validated when presented. Client registrations
  persist across restarts.
- **No secrets in chat.** Bot tokens for Telegram/Discord travel over HTTPS
  to DreamAgent and are stored server-side as project env vars — the same as
  the dashboard's create dialog. The MCP server never logs credentials.
- **Tokens are never stored** by this server — it forwards the caller's own
  Bearer per request and keeps no database.

Privacy: [dreamagent.cloud/privacy](https://dreamagent.cloud/privacy) ·
Terms: [dreamagent.cloud/terms](https://dreamagent.cloud/terms)

## Screenshots

<!-- TODO: add demo GIF (chat creating a bot) + screenshot of connector setup -->

```
Coming soon — a GIF of "Create a Telegram bot" end-to-end from ChatGPT.
```

## Self-hosting / development

The hosted endpoint (`mcp.dreamagent.cloud`) is the easiest way to connect.
To run your own:

```bash
git clone https://github.com/nivethaug/dreamagent-mcp-server
cd dreamagent-mcp-server
pip install -r requirements.txt
cp .env.example .env          # optional: your account for local stdio mode
python server.py              # stdio
python server.py --http --port 8800   # remote MCP (put behind HTTPS)
```

Three modes of authentication, in priority order per request:

1. `Authorization: Bearer da_...` header (OAuth clients / API clients)
2. `?key=da_...` URL parameter (NoAuth custom connectors)
3. `DREAMAGENT_EMAIL` / `DREAMAGENT_PASSWORD` env (personal self-host)

Layout: `server.py` (tools) · `oauth.py` (OAuth 2.1 provider: `/register`,
`/authorize`, `/token`, `/.well-known/*`) · `dreamagent_client.py` (API
client) · `auth.py` (login mode) · `plugin/` (Codex plugin package).

OAuth client registrations persist in `oauth_clients.json`; extend trusted
gateway callbacks via `TRUSTED_OAUTH_REDIRECTS`.

## Links

- **DreamAgent** — [dreamagent.cloud](https://dreamagent.cloud)
- Documentation — [dreamagent.cloud/documentation](https://dreamagent.cloud/documentation)
- Support — support@dreamagent.cloud

## License

MIT
