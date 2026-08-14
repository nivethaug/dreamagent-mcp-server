"""
DreamAgent MCP — Authentication manager.

Connects a DreamAgent account to the MCP server using the EXISTING
POST /auth/login endpoint. The Bearer token is cached in memory and
refreshed on 401. No backend changes required.
"""

import os
import threading
import logging

import httpx

logger = logging.getLogger("dreamagent.mcp.auth")


class AuthError(Exception):
    """Raised when login fails or the account is not usable."""

    def __init__(self, message: str, status_code: int = 401):
        super().__init__(message)
        self.status_code = status_code


class AuthManager:
    """Caches the DreamAgent session token and re-logins on expiry."""

    def __init__(self, api_url: str, email: str, password: str, timeout: float = 30.0):
        self.api_url = api_url.rstrip("/")
        self.email = email
        self.password = password
        self.timeout = timeout
        self._token: str | None = None
        self._user: dict | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------

    def login(self) -> str:
        """Call POST /auth/login and cache the token."""
        with self._lock:
            payload = {"email": self.email, "password": self.password}
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    resp = client.post(f"{self.api_url}/auth/login", json=payload)
            except httpx.HTTPError as e:
                raise AuthError(f"Cannot reach DreamAgent API ({self.api_url}): {e}", 503)

            if resp.status_code == 403:
                detail = ""
                try:
                    detail = resp.json().get("detail", "")
                except Exception:
                    pass
                raise AuthError(
                    f"Login rejected (403). {detail or 'Email not verified or wrong credentials.'} "
                    "Verify your email at dreamagent.cloud first.",
                    403,
                )
            if resp.status_code == 401:
                raise AuthError("Invalid DreamAgent email or password.", 401)
            if resp.status_code != 200:
                raise AuthError(f"Login failed (HTTP {resp.status_code}): {resp.text[:300]}", resp.status_code)

            data = resp.json()
            token = data.get("token")
            if not token:
                raise AuthError("Login response did not include a token.", 500)

            self._token = token
            self._user = data.get("user") or {}
            logger.info("DreamAgent login OK (user id=%s)", self._user.get("id"))
            return token

    # ------------------------------------------------------------------
    # Token access
    # ------------------------------------------------------------------

    def get_token(self) -> str:
        """Return the cached token, logging in if needed."""
        with self._lock:
            if self._token:
                return self._token
        return self.login()

    def invalidate(self) -> None:
        """Drop the cached token (called after a 401 so next call re-logins)."""
        with self._lock:
            self._token = None

    @property
    def user(self) -> dict:
        return self._user or {}

    # ------------------------------------------------------------------
    # Config helpers
    # ------------------------------------------------------------------

    @classmethod
    def from_env(cls) -> "AuthManager":
        """Build from DREAMAGENT_* environment variables."""
        api_url = os.getenv("DREAMAGENT_API_URL", "https://api.dreamagent.cloud")
        email = os.getenv("DREAMAGENT_EMAIL", "")
        password = os.getenv("DREAMAGENT_PASSWORD", "")
        if not email or not password:
            raise AuthError(
                "DREAMAGENT_EMAIL and DREAMAGENT_PASSWORD must be set "
                "(put them in mcp-server/.env or the environment).",
                500,
            )
        return cls(api_url=api_url, email=email, password=password)
