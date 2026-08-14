"""
DreamAgent MCP — OAuth 2.1 Authorization Server.

Lets ChatGPT / Claude listed apps use the standard "Connect" flow:

    ChatGPT ── DCR ──► POST /register            (client_id/secret)
    ChatGPT ─────────► GET  /authorize?...       (login + consent page)
    User logs in with DreamAgent email/password
    Server ──creates──► DreamAgent API key (da_...) via POST /api/keys
    Server ─302───────► redirect_uri?code=...&state=...
    ChatGPT ─────────► POST /token (code + PKCE verifier)
    Server ──────────► {"access_token": "da_..."}   ← a REAL DreamAgent key

The access token IS a DreamAgent API key, so the MCP server's existing
per-request `Authorization: Bearer` handling forwards it unchanged — no
token translation layer, revocation works via the dashboard's key list.

Endpoints (all on the same fastmcp HTTP app):
    GET  /.well-known/oauth-authorization-server[.json|/mcp]
    GET  /.well-known/oauth-protected-resource[/mcp]
    POST /register     — RFC 7591 dynamic client registration
    GET  /authorize    — login + consent page
    POST /authorize    — credential check → code → redirect
    POST /token        — authorization_code + PKCE (S256) exchange
"""

import base64
import hashlib
import html
import json
import logging
import os
import secrets
import time
from typing import Optional
from urllib.parse import urlencode, quote

import httpx
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

logger = logging.getLogger("dreamagent.mcp.oauth")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

OAUTH_BASE_URL = os.getenv("OAUTH_BASE_URL", "https://mcp.dreamagent.cloud")  # issuer
DREAMAGENT_API = os.getenv("DREAMAGENT_API_URL", "https://api.dreamagent.cloud")

# Durable client registry — survives restarts so ChatGPT/Claude connectors
# don't have to re-register after every deploy. (Auth codes stay in-memory:
# they're single-use and expire in 120s anyway.)
CLIENTS_FILE = os.getenv(
    "OAUTH_CLIENTS_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "oauth_clients.json"),
)
CODE_TTL_SECONDS = 120
KEY_NAME_PREFIX = "ChatGPT/Claude OAuth"
LOGIN_RATE_LIMIT = 10          # attempts per IP per window
LOGIN_RATE_WINDOW = 300        # seconds


class _Stores:
    """In-memory OAuth state (single-instance; codes are short-lived)."""

    def __init__(self):
        self.clients: dict = {}   # client_id -> {secret, redirect_uris, name}
        self.codes: dict = {}     # code -> {client_id, redirect_uri, challenge, key, exp}
        self.login_attempts: dict = {}  # ip -> [timestamps]


STORE = _Stores()


def _save_clients() -> None:
    """Persist the client registry (called after each DCR)."""
    try:
        with open(CLIENTS_FILE, "w", encoding="utf-8") as f:
            json.dump(STORE.clients, f)
    except Exception as e:
        logger.warning("oauth: could not persist clients: %s", e)


def _load_clients() -> None:
    """Restore the client registry at boot."""
    try:
        with open(CLIENTS_FILE, "r", encoding="utf-8") as f:
            STORE.clients = json.load(f)
        logger.info("oauth: loaded %d persisted client(s)", len(STORE.clients))
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning("oauth: could not load clients (%s) — starting empty", e)


_load_clients()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _pkce_matches(verifier: str, challenge: str, method: str) -> bool:
    if method != "S256":
        return False
    return _b64url(hashlib.sha256(verifier.encode()).digest()) == challenge


def _rate_limited(ip: str) -> bool:
    now = time.time()
    attempts = [t for t in STORE.login_attempts.get(ip, []) if now - t < LOGIN_RATE_WINDOW]
    STORE.login_attempts[ip] = attempts
    return len(attempts) >= LOGIN_RATE_LIMIT


def _record_attempt(ip: str) -> None:
    STORE.login_attempts.setdefault(ip, []).append(time.time())


async def _dreamagent_login(email: str, password: str) -> Optional[dict]:
    """Verify DreamAgent credentials; return {token, user} or None."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{DREAMAGENT_API}/auth/login",
                json={"email": email, "password": password},
            )
    except httpx.HTTPError as e:
        logger.warning("oauth login call failed: %s", e)
        return None
    if resp.status_code != 200:
        return None
    data = resp.json()
    if not data.get("token"):
        return None
    return data


async def _create_api_key(session_token: str) -> Optional[str]:
    """Create a DreamAgent API key for the logged-in user; return da_... key."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{DREAMAGENT_API}/api/keys",
                json={"name": f"{KEY_NAME_PREFIX} {time.strftime('%Y-%m-%d')}"},
                headers={"Authorization": f"Bearer {session_token}"},
            )
    except httpx.HTTPError as e:
        logger.warning("oauth key creation failed: %s", e)
        return None
    if resp.status_code not in (200, 201):
        return None
    return resp.json().get("key")


def _prune_codes() -> None:
    now = time.time()
    for code in [c for c, v in STORE.codes.items() if v["exp"] < now]:
        STORE.codes.pop(code, None)


# ---------------------------------------------------------------------------
# Route handlers (registered via @mcp.custom_route in server.py)
# ---------------------------------------------------------------------------

async def discovery_authorization_server(request: Request) -> JSONResponse:
    base = OAUTH_BASE_URL
    return JSONResponse({
        "issuer": base,
        "authorization_endpoint": f"{base}/authorize",
        "token_endpoint": f"{base}/token",
        "registration_endpoint": f"{base}/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none", "client_secret_post"],
        "scopes_supported": ["dreamagent"],
        "service_documentation": "https://dreamagent.cloud/documentation",
    })


async def discovery_protected_resource(request: Request) -> JSONResponse:
    base = OAUTH_BASE_URL
    return JSONResponse({
        "resource": f"{base}/mcp",
        "authorization_servers": [base],
    })


async def dynamic_register(request: Request) -> Response:
    """RFC 7591 dynamic client registration (ChatGPT/Claude use this)."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_client_metadata"}, status_code=400)

    redirect_uris = body.get("redirect_uris") or []
    if not isinstance(redirect_uris, list) or not redirect_uris or \
            not all(isinstance(u, str) and u.startswith("https://") for u in redirect_uris):
        return JSONResponse(
            {"error": "invalid_redirect_uri",
             "error_description": "redirect_uris must be non-empty https URLs"},
            status_code=400)

    client_id = f"da-mcp-{secrets.token_hex(12)}"
    client_secret = _b64url(secrets.token_bytes(32))
    STORE.clients[client_id] = {
        "secret": client_secret,
        "redirect_uris": redirect_uris,
        "name": str(body.get("client_name") or "MCP client")[:100],
        "created": time.time(),
    }
    logger.info("oauth: registered client %s (%s) uris=%s",
                client_id, STORE.clients[client_id]["name"], redirect_uris)
    _save_clients()

    return JSONResponse({
        "client_id": client_id,
        "client_secret": client_secret,
        "client_id_issued_at": int(time.time()),
        "redirect_uris": redirect_uris,
        "token_endpoint_auth_method": "client_secret_post",
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
    }, status_code=201)


_AUTHORIZE_FORM = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Connect DreamAgent</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
         display: flex; min-height: 100vh; margin: 0; align-items: center; justify-content: center;
         background: #0f1117; color: #e7e9ee; }}
  .card {{ background: #171a23; border: 1px solid #262b38; border-radius: 16px;
          padding: 32px; width: 100%; max-width: 380px; margin: 16px; }}
  h1 {{ font-size: 20px; margin: 0 0 4px;
       background: linear-gradient(135deg,#6366f1,#8b5cf6); -webkit-background-clip: text;
       -webkit-text-fill-color: transparent; }}
  p.sub {{ color: #9aa1af; font-size: 13px; margin: 0 0 20px; }}
  ul {{ color: #9aa1af; font-size: 13px; margin: 0 0 20px; padding-left: 18px; }}
  label {{ display: block; font-size: 12px; color: #9aa1af; margin: 12px 0 4px; }}
  input[type=email], input[type=password] {{ width: 100%; box-sizing: border-box;
      padding: 10px 12px; border-radius: 10px; border: 1px solid #2c3242;
      background: #0f1117; color: #e7e9ee; font-size: 14px; }}
  button {{ width: 100%; margin-top: 20px; padding: 12px; border: 0; border-radius: 10px;
      background: linear-gradient(135deg,#6366f1,#8b5cf6); color: #fff; font-weight: 600;
      font-size: 14px; cursor: pointer; }}
  .err {{ color: #f87171; font-size: 13px; margin-top: 12px; white-space: pre-line; }}
  .foot {{ color: #6b7280; font-size: 11px; margin-top: 16px; text-align: center; }}
</style></head><body>
<div class="card">
  <h1>DreamAgent</h1>
  <p class="sub">Connect your account to <b>{client_name}</b></p>
  <ul>
    <li>Build &amp; edit your apps and bots</li>
    <li>Read project status and logs</li>
    <li>Uses your plan's AI credits</li>
  </ul>
  {hidden}
  <label for="email">DreamAgent email</label>
  <input id="email" name="email" type="email" autocomplete="username" required autofocus>
  <label for="password">Password</label>
  <input id="password" name="password" type="password" autocomplete="current-password" required>
  {error_html}
  <button type="submit">Sign in &amp; Connect</button>
  <p class="foot">A revocable DreamAgent API key will be created for this connection.<br>
     Manage keys anytime in DreamAgent &rarr; Settings.</p>
</div>
</body></html>"""


def _authorize_page(client_name: str, fields: dict, error: Optional[str]) -> str:
    hidden = "".join(
        f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(v)}">'
        for k, v in fields.items() if v is not None
    )
    return "<form method=\"post\" action=\"/authorize\">" + _AUTHORIZE_FORM.format(
        client_name=html.escape(client_name),
        hidden=hidden,
        error_html=f'<p class="err">{html.escape(error)}</p>' if error else "",
    ) + "</form>"


def _validate_authorize_params(q) -> tuple[Optional[dict], Optional[str]]:
    """Common validation for GET and POST of /authorize."""
    client_id = q.get("client_id")
    redirect_uri = q.get("redirect_uri")
    response_type = q.get("response_type")
    code_challenge = q.get("code_challenge")
    # NB: `or` (not dict-default) — callers may pass the key with value None
    # (form submissions don't include the method field).
    challenge_method = q.get("code_challenge_method") or "S256"

    client = STORE.clients.get(client_id or "")
    if not client:
        return None, "Unknown client_id — the connector must re-register."
    if not redirect_uri or redirect_uri not in client["redirect_uris"]:
        return None, "redirect_uri does not match the registered redirect URIs."
    if response_type != "code":
        return None, "Only response_type=code is supported."
    if not code_challenge or challenge_method != "S256":
        return None, "PKCE with code_challenge_method=S256 is required."

    return {
        "client_id": client_id,
        "client_name": client["name"],
        "response_type": "code",          # echoed into the form hidden fields
        "redirect_uri": redirect_uri,
        "state": q.get("state"),
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",  # echoed into the form hidden fields
    }, None


async def authorize_get(request: Request) -> Response:
    fields, err = _validate_authorize_params(request.query_params)
    if not fields:
        return HTMLResponse(_authorize_page("ChatGPT", {}, err), status_code=400)
    return HTMLResponse(_authorize_page(fields["client_name"], fields, None))


async def authorize_post(request: Request) -> Response:
    form = await request.form()
    q = {k: form.get(k) for k in (
        "client_id", "redirect_uri", "response_type", "state",
        "code_challenge", "code_challenge_method", "email", "password")}
    fields, err = _validate_authorize_params(q)
    if not fields:
        return HTMLResponse(_authorize_page("ChatGPT", {}, err), status_code=400)

    redirect_uri = fields["redirect_uri"]

    ip = request.client.host if request.client else "?"
    if _rate_limited(ip):
        logger.warning("oauth: rate limited login from %s", ip)
        # Redirect with error per OAuth spec (user-visible page is confusing mid-abuse)
        sep = "&" if "?" in redirect_uri else "?"
        return RedirectResponse(f"{redirect_uri}{sep}error=access_denied", status_code=302)
    _record_attempt(ip)

    email = (q.get("email") or "").strip()
    password = q.get("password") or ""
    if not email or not password:
        return HTMLResponse(_authorize_page(
            fields["client_name"], fields, "Enter your email and password."))

    login = await _dreamagent_login(email, password)
    if not login:
        logger.info("oauth: failed login for %s from %s", email[:3] + "***", ip)
        return HTMLResponse(_authorize_page(
            fields["client_name"], fields,
            "Invalid email or password (or email not verified)."))

    da_key = await _create_api_key(login["token"])
    if not da_key:
        return HTMLResponse(_authorize_page(
            fields["client_name"], fields,
            "Could not create an API key for your account. Try again."))

    _prune_codes()
    code = secrets.token_urlsafe(32)
    STORE.codes[code] = {
        "client_id": fields["client_id"],
        "redirect_uri": redirect_uri,
        "challenge": fields["code_challenge"],
        "key": da_key,
        "exp": time.time() + CODE_TTL_SECONDS,
    }
    logger.info("oauth: authorized user=%s key=%s... for client %s",
                login.get("user", {}).get("id"), da_key[:8], fields["client_id"])

    params = {"code": code}
    if fields.get("state"):
        params["state"] = fields["state"]
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(f"{redirect_uri}{sep}{urlencode(params)}", status_code=302)


async def token(request: Request) -> Response:
    form = await request.form()
    grant_type = form.get("grant_type")
    if grant_type != "authorization_code":
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

    client_id = form.get("client_id")
    client_secret = form.get("client_secret")
    code = form.get("code")
    redirect_uri = form.get("redirect_uri")
    verifier = form.get("code_verifier")

    client = STORE.clients.get(client_id or "")
    if not client:
        return JSONResponse({"error": "invalid_client"}, status_code=401)
    if client_secret is not None and client_secret != client["secret"]:
        return JSONResponse({"error": "invalid_client"}, status_code=401)

    _prune_codes()
    entry = STORE.codes.get(code or "")
    if not entry:
        return JSONResponse({"error": "invalid_grant",
                             "error_description": "code is invalid, expired, or already used"},
                            status_code=400)
    STORE.codes.pop(code, None)  # single use

    if entry["client_id"] != client_id or entry["redirect_uri"] != redirect_uri:
        return JSONResponse({"error": "invalid_grant"}, status_code=400)
    if not verifier or not _pkce_matches(verifier, entry["challenge"], "S256"):
        return JSONResponse({"error": "invalid_grant",
                             "error_description": "PKCE verification failed"}, status_code=400)

    logger.info("oauth: token issued to client %s (key %s...)", client_id, entry["key"][:8])
    return JSONResponse({
        "access_token": entry["key"],
        "token_type": "Bearer",
        "scope": "dreamagent",
    })


def register_oauth_routes(mcp) -> None:
    """Attach all OAuth routes to the fastmcp app."""
    mcp.custom_route("/.well-known/oauth-authorization-server", methods=["GET"])(
        discovery_authorization_server)
    mcp.custom_route("/.well-known/oauth-authorization-server/mcp", methods=["GET"])(
        discovery_authorization_server)
    mcp.custom_route("/.well-known/oauth-protected-resource", methods=["GET"])(
        discovery_protected_resource)
    mcp.custom_route("/.well-known/oauth-protected-resource/mcp", methods=["GET"])(
        discovery_protected_resource)
    mcp.custom_route("/register", methods=["POST"])(dynamic_register)
    mcp.custom_route("/authorize", methods=["GET"])(authorize_get)
    mcp.custom_route("/authorize", methods=["POST"])(authorize_post)
    mcp.custom_route("/token", methods=["POST"])(token)
