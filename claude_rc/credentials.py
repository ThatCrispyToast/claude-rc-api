"""OAuth credential handling for the Claude Remote Control / Sessions API.

The Claude Code CLI logs in through claude.ai (OAuth) and stores the resulting
tokens in ``~/.claude/.credentials.json`` and account metadata (including the
organization UUID that the API requires as the ``x-organization-uuid`` header)
in ``~/.claude.json``.

This module reads those files so a third-party program can authenticate exactly
the way the claude.ai/code web app does, and refreshes the access token against
``POST https://api.anthropic.com/v1/oauth/token`` when it has expired.

All of the constants below were reverse-engineered from the CLI binary:

* client id          -> ``9d1c250a-e61b-44d9-88ed-5944d1962f5e``
* token endpoint     -> ``https://api.anthropic.com/v1/oauth/token``
* oauth beta header  -> ``oauth-2025-04-20``
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx

# --- reverse-engineered constants ------------------------------------------
CLAUDE_CODE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
# Token refresh is served by platform.claude.com, NOT api.anthropic.com.
OAUTH_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
OAUTH_BETA = "oauth-2025-04-20"
# The CLI's own refresh User-Agent, for parity.
OAUTH_USER_AGENT = "anthropic-sdk-typescript/0.94.0 userOAuthProvider"

DEFAULT_CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"
DEFAULT_CONFIG_PATH = Path.home() / ".claude.json"

# Environment overrides (handy in CI / other machines).
ENV_ACCESS_TOKEN = "CLAUDE_RC_ACCESS_TOKEN"
ENV_ORG_UUID = "CLAUDE_RC_ORG_UUID"
# The CLI's own env override for a bare access token.
ENV_CLI_OAUTH_TOKEN = "CLAUDE_CODE_OAUTH_TOKEN"


def _now_ms() -> int:
    return int(time.time() * 1000)


class CredentialsError(RuntimeError):
    """Raised when credentials cannot be located, read, or refreshed."""


@dataclass
class OAuthCredentials:
    """The ``claudeAiOauth`` block of ``~/.claude/.credentials.json``."""

    access_token: str
    refresh_token: Optional[str] = None
    expires_at_ms: Optional[int] = None
    scopes: list[str] = field(default_factory=list)
    subscription_type: Optional[str] = None
    client_id: Optional[str] = None
    trusted_device_token: Optional[str] = None
    source_path: Optional[Path] = None

    def is_expired(self, skew_seconds: int = 60) -> bool:
        """True if the token is expired (or within ``skew_seconds`` of it)."""
        if not self.expires_at_ms:
            return False
        return _now_ms() >= self.expires_at_ms - skew_seconds * 1000

    # -- (de)serialization -------------------------------------------------
    @classmethod
    def from_block(
        cls,
        block: dict,
        source_path: Optional[Path] = None,
        trusted_device_token: Optional[str] = None,
    ) -> "OAuthCredentials":
        token = block.get("accessToken")
        if not token:
            raise CredentialsError("credentials file has no claudeAiOauth.accessToken")
        return cls(
            access_token=token,
            refresh_token=block.get("refreshToken"),
            expires_at_ms=block.get("expiresAt"),
            scopes=list(block.get("scopes") or []),
            subscription_type=block.get("subscriptionType"),
            client_id=block.get("clientId"),
            trusted_device_token=trusted_device_token,
            source_path=source_path,
        )

    def to_block(self) -> dict:
        block = {
            "accessToken": self.access_token,
            "refreshToken": self.refresh_token,
            "expiresAt": self.expires_at_ms,
            "scopes": self.scopes,
            "subscriptionType": self.subscription_type,
        }
        if self.client_id:
            block["clientId"] = self.client_id
        return block


def load_credentials(path: os.PathLike | str | None = None) -> OAuthCredentials:
    """Load OAuth credentials.

    Priority:
      1. ``CLAUDE_RC_ACCESS_TOKEN`` env var (no refresh possible).
      2. ``~/.claude/.credentials.json`` (``claudeAiOauth`` block).
    """
    env_token = os.environ.get(ENV_ACCESS_TOKEN) or os.environ.get(ENV_CLI_OAUTH_TOKEN)
    if env_token:
        return OAuthCredentials(access_token=env_token)

    p = Path(path) if path else DEFAULT_CREDENTIALS_PATH
    if not p.exists():
        raise CredentialsError(
            f"credentials file not found at {p}. Run `claude` and `/login` first, "
            f"or set {ENV_ACCESS_TOKEN}."
        )
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as exc:  # pragma: no cover - io
        raise CredentialsError(f"could not read {p}: {exc}") from exc

    block = data.get("claudeAiOauth")
    if not block:
        raise CredentialsError(
            f"{p} has no `claudeAiOauth` block — are you logged in via an API key "
            f"instead of claude.ai? Remote Control requires a claude.ai OAuth login."
        )
    # A top-level `trustedDeviceToken` sibling is used when the org requires
    # Trusted Devices; harmless to carry when absent.
    return OAuthCredentials.from_block(
        block, source_path=p, trusted_device_token=data.get("trustedDeviceToken")
    )


def load_org_uuid(config_path: os.PathLike | str | None = None) -> Optional[str]:
    """Read ``oauthAccount.organizationUuid`` from ``~/.claude.json``.

    Returns ``None`` if it cannot be found (fall back to ``CLAUDE_RC_ORG_UUID``).
    """
    env_org = os.environ.get(ENV_ORG_UUID)
    if env_org:
        return env_org

    p = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    if p.exists():
        try:
            data = json.loads(p.read_text())
            account = data.get("oauthAccount") or {}
            if account.get("organizationUuid"):
                return account["organizationUuid"]
        except (OSError, json.JSONDecodeError):
            pass

    # Fallback: a top-level `organizationUuid` sibling in .credentials.json.
    cp = DEFAULT_CREDENTIALS_PATH
    if cp.exists():
        try:
            data = json.loads(cp.read_text())
            return data.get("organizationUuid")
        except (OSError, json.JSONDecodeError):
            return None
    return None


def refresh_credentials(
    creds: OAuthCredentials,
    *,
    persist: bool = True,
    client: httpx.Client | None = None,
) -> OAuthCredentials:
    """Refresh an expired access token via the OAuth token endpoint.

    Mirrors the CLI: ``POST /v1/oauth/token`` with a JSON body of
    ``{grant_type, refresh_token, client_id}`` and the ``oauth-2025-04-20`` beta
    header. On success the new tokens are (optionally) written back to the same
    credentials file so subsequent runs stay authenticated.
    """
    if not creds.refresh_token:
        raise CredentialsError(
            "access token is expired and no refresh_token is available "
            "(token came from an env var). Re-run `claude` to refresh it."
        )

    owns_client = client is None
    client = client or httpx.Client(timeout=30.0)
    try:
        resp = client.post(
            OAUTH_TOKEN_URL,
            headers={
                "Content-Type": "application/json",
                "anthropic-beta": OAUTH_BETA,
                "User-Agent": OAUTH_USER_AGENT,
            },
            json={
                "grant_type": "refresh_token",
                "refresh_token": creds.refresh_token,
                "client_id": creds.client_id or CLAUDE_CODE_CLIENT_ID,
            },
        )
    finally:
        if owns_client:
            client.close()

    if resp.status_code != 200:
        raise CredentialsError(
            f"token refresh failed: HTTP {resp.status_code} {resp.text[:200]}"
        )
    body = resp.json()
    new = OAuthCredentials(
        access_token=body["access_token"],
        refresh_token=body.get("refresh_token", creds.refresh_token),
        expires_at_ms=_now_ms() + int(body.get("expires_in", 0)) * 1000
        if body.get("expires_in")
        else creds.expires_at_ms,
        scopes=(body.get("scope") or " ".join(creds.scopes)).split()
        if body.get("scope")
        else creds.scopes,
        subscription_type=creds.subscription_type,
        client_id=creds.client_id,
        trusted_device_token=creds.trusted_device_token,
        source_path=creds.source_path,
    )

    if persist and new.source_path:
        _persist(new)
    return new


def _persist(creds: OAuthCredentials) -> None:
    """Atomically write refreshed tokens back, preserving other keys."""
    path = creds.source_path
    if not path:
        return
    try:
        data = json.loads(path.read_text()) if path.exists() else {}
    except (OSError, json.JSONDecodeError):
        data = {}
    data["claudeAiOauth"] = creds.to_block()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
