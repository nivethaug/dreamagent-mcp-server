"""
DreamAgent MCP — HTTP client for the EXISTING DreamAgent REST API.

Every method maps 1:1 to an existing endpoint on api.dreamagent.cloud.
No backend modifications. The user's own token is forwarded, so all
server-side ownership checks apply unchanged.
"""

import json
import logging
import threading
import time
from contextvars import ContextVar
from typing import Optional

import httpx

from auth import AuthManager, AuthError

logger = logging.getLogger("dreamagent.mcp.client")

# Poll-friendly defaults: creation + AI edits are long-running server-side,
# but each individual HTTP call here stays short.
DEFAULT_TIMEOUT = 30.0
CHAT_STREAM_TIMEOUT = 1800.0  # background SSE consumer may run ~30 min

# Per-request token (hosted mode): set from the incoming MCP request's
# Authorization header by server.py before each tool call. Takes priority
# over the env-credential AuthManager.
_current_request_token: ContextVar[str | None] = ContextVar(
    "dreamagent_request_token", default=None)


def set_request_token(token: str | None) -> None:
    """Bind the incoming request's Bearer token (hosted mode)."""
    _current_request_token.set(token)


def get_request_token() -> str | None:
    return _current_request_token.get()


# Project type name -> type_id (from project_types seed, verified in codebase)
PROJECT_TYPES = {
    "website": 1,
    "telegrambot": 2,
    "discordbot": 3,
    "tradingbot": 4,
    "scheduler": 5,
}


class DreamAgentAPIError(Exception):
    """Readable error surfaced to ChatGPT."""

    def __init__(self, message: str, status_code: int = 500):
        super().__init__(message)
        self.status_code = status_code


class DreamAgentClient:
    def __init__(self, auth: AuthManager):
        self.auth = auth
        self.base_url = auth.api_url
        # Local best-effort store of finished chat jobs: {session_key: {...}}
        self.chat_results: dict = {}
        self._chat_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Core request helper (auto re-login once on 401)
    # ------------------------------------------------------------------

    def _request(self, method: str, path: str, *, json_body=None, params=None,
                 timeout: float = DEFAULT_TIMEOUT, _retried: bool = False) -> httpx.Response:
        # Hosted mode: per-request token from the incoming MCP Authorization
        # header (the user's DreamAgent API key). Local mode: env-credential
        # login token.
        request_token = get_request_token()
        if request_token:
            token = request_token
        else:
            token = self.auth.get_token()
        headers = {"Authorization": f"Bearer {token}"}
        try:
            with httpx.Client(timeout=timeout) as client:
                resp = client.request(
                    method, f"{self.base_url}{path}",
                    headers=headers, json=json_body, params=params,
                )
        except httpx.HTTPError as e:
            raise DreamAgentAPIError(f"Cannot reach DreamAgent API: {e}", 503)

        if resp.status_code == 401 and not _retried:
            if request_token:
                # Hosted mode: the user's API key was rejected — no re-login possible.
                raise DreamAgentAPIError(
                    "DreamAgent rejected your API key (401). It may have been revoked — "
                    "create a new key in dreamagent.cloud Settings → Connect to ChatGPT "
                    "and update it in your ChatGPT connector settings.", 401)
            # Local mode: token expired/revoked — re-login once and retry.
            self.auth.invalidate()
            return self._request(method, path, json_body=json_body, params=params,
                                 timeout=timeout, _retried=True)
        return resp

    @staticmethod
    def _friendly_error(resp: httpx.Response, action: str) -> DreamAgentAPIError:
        detail = ""
        try:
            body = resp.json()
            detail = body.get("detail") or body.get("error") or json.dumps(body)[:300]
            if isinstance(detail, dict):
                detail = detail.get("message") or detail.get("error") or json.dumps(detail)[:300]
        except Exception:
            detail = resp.text[:300]

        hints = {
            402: "Insufficient AI credits. Top up at dreamagent.cloud/billing.",
            403: "Not allowed (plan limit or not your project).",
            404: "Project not found (check the id with dreamagent_list_projects).",
            409: "Conflict: another creation/edit is already active. Wait for it to finish.",
            423: "Session locked: another chat session owns this project. Wait or release it in the dashboard.",
        }
        hint = hints.get(resp.status_code, "")
        msg = f"{action} failed (HTTP {resp.status_code}): {detail}"
        if hint:
            msg += f" — {hint}"
        return DreamAgentAPIError(msg, resp.status_code)

    # ------------------------------------------------------------------
    # GET /projects
    # ------------------------------------------------------------------

    def list_projects(self, status_filter: str | None = None) -> list[dict]:
        resp = self._request("GET", "/projects")
        if resp.status_code != 200:
            raise self._friendly_error(resp, "Listing projects")
        projects = resp.json()
        if status_filter:
            projects = [p for p in projects if (p.get("status") or "") == status_filter]
        return projects

    # ------------------------------------------------------------------
    # POST /projects
    # ------------------------------------------------------------------

    def create_project(self, name: str, project_type: str, bot_token: str | None,
                       description: str | None = None, env_vars: dict | None = None,
                       bot_token_integration_id: int | None = None,
                       global_integration_ids: list | None = None) -> dict:
        type_id = PROJECT_TYPES.get(project_type.lower())
        if type_id is None:
            raise DreamAgentAPIError(
                f"Unknown project type '{project_type}'. Valid: {', '.join(sorted(PROJECT_TYPES))}.", 400)

        payload: dict = {"name": name[:30], "type_id": type_id}
        if description:
            payload["description"] = description
        if type_id in (2, 3):  # telegram / discord bots require a token
            if bot_token_integration_id:
                payload["bot_token_integration_id"] = bot_token_integration_id
            elif not bot_token:
                raise DreamAgentAPIError(
                    f"A bot_token is required for {project_type} projects — either "
                    "bot_token_integration_id (from dreamagent_list_global_integrations) "
                    "or a raw bot_token.", 400)
            else:
                payload["bot_token"] = bot_token
        if type_id == 5 and bot_token:  # scheduler — bot_token doubles as telegram sender token
            payload["telegram_bot_token"] = bot_token
        if global_integration_ids:
            payload["global_integration_ids"] = global_integration_ids
        if env_vars:
            payload["environment_variables"] = [
                {"key": k, "value": v, "docs_url": ""} for k, v in env_vars.items()
            ]

        resp = self._request("POST", "/projects", json_body=payload, timeout=60.0)
        if resp.status_code not in (200, 201):
            raise self._friendly_error(resp, f"Creating {project_type} project")
        return resp.json()

    # ------------------------------------------------------------------
    # Global Integrations (saved credentials — metadata only, no values)
    # ------------------------------------------------------------------

    def list_global_integrations(self) -> list:
        resp = self._request("GET", "/api/global-integrations")
        if resp.status_code != 200:
            raise self._friendly_error(resp, "Listing global integrations")
        return resp.json()

    # ------------------------------------------------------------------
    # GET /projects/{id}/env — masked env vars + registry metadata
    # ------------------------------------------------------------------

    def list_project_env(self, project_id: int) -> dict:
        resp = self._request("GET", f"/projects/{project_id}/env")
        if resp.status_code != 200:
            raise self._friendly_error(resp, f"Listing env vars of project {project_id}")
        return resp.json()

    # ------------------------------------------------------------------
    # GET /projects/{id}/status
    # ------------------------------------------------------------------

    def get_project_status(self, project_id: int) -> dict:
        resp = self._request("GET", f"/projects/{project_id}/status")
        if resp.status_code != 200:
            raise self._friendly_error(resp, f"Getting status of project {project_id}")
        return resp.json()

    # ------------------------------------------------------------------
    # Sessions + chat
    # ------------------------------------------------------------------

    def list_sessions(self, project_id: int) -> list[dict]:
        resp = self._request("GET", f"/projects/{project_id}/sessions")
        if resp.status_code != 200:
            raise self._friendly_error(resp, f"Listing sessions of project {project_id}")
        return resp.json()

    def create_session(self, project_id: int, label: str = "ChatGPT") -> dict:
        payload = {"label": label, "project_id": project_id}
        resp = self._request("POST", f"/projects/{project_id}/sessions", json_body=payload)
        if resp.status_code not in (200, 201):
            raise self._friendly_error(resp, f"Creating chat session for project {project_id}")
        return resp.json()

    def ensure_session(self, project_id: int) -> dict:
        """Reuse the latest non-archived session, or create a 'ChatGPT' one."""
        sessions = self.list_sessions(project_id)
        active = [s for s in sessions if not s.get("archived")]
        if active:
            return active[0]
        return self.create_session(project_id, label="ChatGPT")

    def ensure_chatgpt_session(self, project_id: int) -> dict:
        """Reuse OUR OWN session for this project — never the user's dashboard
        session (which would hit the project lock → 423).

        Mirrors the platform's Telegram/Discord bot integrations: each client
        manages its own labeled session. Finds the latest non-archived session
        labeled 'ChatGPT', else creates one.
        """
        sessions = self.list_sessions(project_id)
        ours = [s for s in sessions
                if not s.get("archived") and (s.get("label") or "").strip().lower() == "chatgpt"]
        if ours:
            return ours[0]
        return self.create_session(project_id, label="ChatGPT")

    def get_active_session(self, project_id: int) -> dict:
        """Get the session currently holding the project's chat lock."""
        resp = self._request("GET", f"/projects/{project_id}/active-session")
        if resp.status_code != 200:
            raise self._friendly_error(resp, f"Getting active session of project {project_id}")
        return resp.json()

    def resolve_progress_session(self, project_id: int) -> tuple[Optional[str], str]:
        """Find the session to report progress on, without needing a key.

        Preference order:
          1. the session currently holding the project lock (an edit is
             actively running there), matched by id via list_sessions
          2. the latest non-archived 'ChatGPT'-labeled session (ours)
          3. the latest non-archived session of any label
        Returns (session_key or None, how_it_was_found).
        """
        sessions = self.list_sessions(project_id)
        active_id = None
        try:
            active_id = (self.get_active_session(project_id) or {}).get("active_session_id")
        except DreamAgentAPIError:
            pass
        if active_id:
            for s in sessions:
                if s.get("id") == active_id and not s.get("archived"):
                    return s.get("session_key"), "locked session (edit running)"
        non_archived = [s for s in sessions if not s.get("archived")]
        ours = [s for s in non_archived
                if (s.get("label") or "").strip().lower() == "chatgpt"]
        pool = ours or non_archived
        if pool:
            how = "ChatGPT session" if ours else "latest session"
            return pool[0].get("session_key"), how
        return None, "none"

    def release_project_lock(self, project_id: int) -> dict:
        """Force-release the project's chat lock (owner-scoped server-side)."""
        resp = self._request("DELETE", f"/projects/{project_id}/lock")
        if resp.status_code not in (200, 201):
            raise self._friendly_error(resp, f"Releasing lock on project {project_id}")
        return resp.json()

    def submit_chat(self, session_key: str, message: str, wait_seconds: float = 3.0) -> Optional[str]:
        """
        Fire POST /chat/stream in a background thread.

        Waits up to `wait_seconds` for an IMMEDIATE failure (401/402/409/423
        rejected before the run starts) and returns the error string so the
        tool can surface it synchronously. Returns None if the run started
        (or is still connecting) — poll chat_status/local_chat_result after.

        The thread keeps the SSE connection open (driving the run server-side)
        and records the final result locally when the stream ends. Progress is
        ALWAYS also pollable via /chat/status + /chat/chunks (durable runs),
        so this local record is only a convenience.
        """
        with self._chat_lock:
            self.chat_results.pop(session_key, None)

        # Snapshot the token HERE (calling thread): background threads start
        # with an empty contextvars context, so the request token must be
        # captured before the thread spawns.
        request_token = get_request_token()

        def _worker(request_token=request_token):
            payload = {
                "session_key": session_key,
                "messages": [{"role": "user", "content": message}],
                "stream": True,
                "acp_mode": True,
                "mode": "dream",
            }
            token = request_token or self.auth.get_token()
            headers = {"Authorization": f"Bearer {token}"}
            collected: list[str] = []
            error: str | None = None
            try:
                with httpx.Client(timeout=CHAT_STREAM_TIMEOUT) as client:
                    with client.stream("POST", f"{self.base_url}/chat/stream",
                                       headers=headers, json=payload) as resp:
                        if resp.status_code == 401 and not request_token:
                            self.auth.invalidate()
                            token = self.auth.get_token()
                            headers = {"Authorization": f"Bearer {token}"}
                            # Retry once with the fresh token.
                            with client.stream("POST", f"{self.base_url}/chat/stream",
                                               headers=headers, json=payload) as resp2:
                                self._consume_sse(resp2, collected)
                        elif resp.status_code >= 400:
                            body = resp.read().decode("utf-8", "replace")[:400]
                            # Parse the friendly message out of FastAPI error bodies
                            try:
                                detail = json.loads(body).get("detail")
                                if isinstance(detail, dict):
                                    msg = detail.get("message") or detail.get("error") or body
                                    body = msg
                            except Exception:
                                pass
                            hints = {
                                402: " — Insufficient AI credits. Top up at dreamagent.cloud/billing.",
                                409: " — An edit is already running in this session. Poll dreamagent_get_chat_status instead of submitting again.",
                                423: (" — Session locked: another chat session owns this project "
                                      "(likely open in the DreamAgent dashboard). Close it there, "
                                      "or submit with new_session=true / a different session_key."),
                            }
                            hint = hints.get(resp.status_code, "")
                            error = f"HTTP {resp.status_code}: {body}{hint}"
                        else:
                            self._consume_sse(resp, collected)
            except httpx.HTTPError as e:
                # Network drop mid-run is non-fatal: the durable run continues
                # server-side and chunks remain pollable.
                logger.warning("chat stream connection ended: %s", e)
            except Exception as e:  # never crash the worker
                logger.exception("chat worker failed")
                error = str(e)

            with self._chat_lock:
                self.chat_results[session_key] = {
                    "done": True,
                    "error": error,
                    "text": "".join(collected)[-8000:],
                }

        thread = threading.Thread(target=_worker, daemon=True, name=f"chat-{session_key[:8]}")
        thread.start()

        # Wait briefly for an immediate failure (rejected before the run
        # started: 401/402/409/423) so the tool can report it synchronously
        # instead of a false "submitted".
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            r = self.local_chat_result(session_key)
            if r is not None:
                if r.get("error") and not r.get("text"):
                    return r["error"]
                return None  # finished cleanly/immediately
            time.sleep(0.1)
        return None

    def _consume_sse(self, resp, collected: list[str]) -> None:
        """Best-effort SSE consumption: keep connection alive, collect text."""
        for line in resp.iter_lines():
            if not line or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except Exception:
                collected.append(data)
                continue
            # Known shapes: {"choices":[{"delta":{"content": ...}}]} or {"chunk": ...}
            try:
                choices = obj.get("choices")
                if choices:
                    text = (choices[0].get("delta") or {}).get("content")
                    if text:
                        collected.append(text)
                        continue
                if isinstance(obj.get("chunk"), str):
                    collected.append(obj["chunk"])
            except Exception:
                pass

    def chat_status(self, session_key: str) -> dict:
        resp = self._request("GET", "/chat/status", params={"session_key": session_key})
        if resp.status_code != 200:
            raise self._friendly_error(resp, "Checking chat status")
        return resp.json()

    def chat_chunks(self, session_key: str, after: int = 0) -> dict:
        resp = self._request("GET", "/chat/chunks",
                             params={"session_key": session_key, "after": after})
        if resp.status_code != 200:
            raise self._friendly_error(resp, "Fetching chat progress")
        return resp.json()

    def cancel_chat(self, session_key: str) -> dict:
        resp = self._request("POST", "/chat/cancel",
                             json_body={"session_key": session_key})
        if resp.status_code != 200:
            raise self._friendly_error(resp, "Cancelling chat")
        return resp.json()

    def local_chat_result(self, session_key: str) -> dict | None:
        with self._chat_lock:
            return self.chat_results.get(session_key)
