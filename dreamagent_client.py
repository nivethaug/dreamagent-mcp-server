"""
DreamAgent MCP — HTTP client for the EXISTING DreamAgent REST API.

Every method maps 1:1 to an existing endpoint on api.dreamagent.cloud.
No backend modifications. The user's own token is forwarded, so all
server-side ownership checks apply unchanged.
"""

import json
import logging
import threading

import httpx

from auth import AuthManager, AuthError

logger = logging.getLogger("dreamagent.mcp.client")

# Poll-friendly defaults: creation + AI edits are long-running server-side,
# but each individual HTTP call here stays short.
DEFAULT_TIMEOUT = 30.0
CHAT_STREAM_TIMEOUT = 1800.0  # background SSE consumer may run ~30 min

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
            # Token expired/revoked — re-login once and retry.
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
                       description: str | None = None, env_vars: dict | None = None) -> dict:
        type_id = PROJECT_TYPES.get(project_type.lower())
        if type_id is None:
            raise DreamAgentAPIError(
                f"Unknown project type '{project_type}'. Valid: {', '.join(sorted(PROJECT_TYPES))}.", 400)

        payload: dict = {"name": name[:30], "type_id": type_id}
        if description:
            payload["description"] = description
        if type_id in (2, 3):  # telegram / discord bots require a token
            if not bot_token:
                raise DreamAgentAPIError(
                    f"A bot_token is required for {project_type} projects "
                    "(the Telegram/Discord bot token).", 400)
            payload["bot_token"] = bot_token
        if type_id == 5:  # scheduler — bot_token doubles as telegram sender token
            if bot_token:
                payload["telegram_bot_token"] = bot_token
        if env_vars:
            payload["environment_variables"] = [
                {"key": k, "value": v, "docs_url": ""} for k, v in env_vars.items()
            ]

        resp = self._request("POST", "/projects", json_body=payload, timeout=60.0)
        if resp.status_code not in (200, 201):
            raise self._friendly_error(resp, f"Creating {project_type} project")
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

    def submit_chat(self, session_key: str, message: str) -> None:
        """
        Fire POST /chat/stream in a background thread and return immediately.

        The thread keeps the SSE connection open (driving the run server-side)
        and records the final result locally when the stream ends. Progress is
        ALWAYS also pollable via /chat/status + /chat/chunks (durable runs),
        so this local record is only a convenience.
        """
        with self._chat_lock:
            self.chat_results.pop(session_key, None)

        def _worker():
            payload = {
                "session_key": session_key,
                "messages": [{"role": "user", "content": message}],
                "stream": True,
                "acp_mode": True,
                "mode": "dream",
            }
            token = self.auth.get_token()
            headers = {"Authorization": f"Bearer {token}"}
            collected: list[str] = []
            error: str | None = None
            try:
                with httpx.Client(timeout=CHAT_STREAM_TIMEOUT) as client:
                    with client.stream("POST", f"{self.base_url}/chat/stream",
                                       headers=headers, json=payload) as resp:
                        if resp.status_code == 401:
                            self.auth.invalidate()
                            token = self.auth.get_token()
                            headers = {"Authorization": f"Bearer {token}"}
                            # Retry once with the fresh token.
                            with client.stream("POST", f"{self.base_url}/chat/stream",
                                               headers=headers, json=payload) as resp2:
                                self._consume_sse(resp2, collected)
                        elif resp.status_code >= 400:
                            body = resp.read().decode("utf-8", "replace")[:400]
                            error = f"HTTP {resp.status_code}: {body}"
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

        threading.Thread(target=_worker, daemon=True, name=f"chat-{session_key[:8]}").start()

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
