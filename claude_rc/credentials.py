"""OAuth credential handling for the Claude Remote Control / Sessions API.

The Claude Code CLI logs in through claude.ai (OAuth) and stores the resulting
tokens in ``~/.claude/.credentials.json`` (on macOS, in the login Keychain item
``Claude Code-credentials`` instead) and account metadata (including the
organization UUID that the API requires as the ``x-organization-uuid`` header)
in ``~/.claude.json``.

This module reads those stores so a third-party program can authenticate exactly
the way the claude.ai/code web app does, and refreshes the access token against
``POST https://platform.claude.com/v1/oauth/token`` when it has expired.

All of the constants below were reverse-engineered from the CLI binary:

* client id          -> ``9d1c250a-e61b-44d9-88ed-5944d1962f5e``
* token endpoint     -> ``https://platform.claude.com/v1/oauth/token``
  (refresh is served by platform.claude.com, *not* api.anthropic.com)
* oauth beta header  -> ``oauth-2025-04-20``
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx

from ._version import __version__

_log = logging.getLogger(__name__)

# --- reverse-engineered constants ------------------------------------------
CLAUDE_CODE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
# Token refresh is served by platform.claude.com, NOT api.anthropic.com.
OAUTH_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
OAUTH_BETA = "oauth-2025-04-20"
# The CLI's own refresh User-Agent, for parity.
OAUTH_USER_AGENT = "anthropic-sdk-typescript/0.94.0 userOAuthProvider"

DEFAULT_CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"
DEFAULT_CONFIG_PATH = Path.home() / ".claude.json"
# On macOS the CLI keeps the same JSON document (``claudeAiOauth`` block and
# all) as a login-Keychain generic password under this service name. It reads
# the Keychain first; the credentials file is only its fallback (a Keychain
# write that failed).
KEYCHAIN_SERVICE = "Claude Code-credentials"
# The account it files the item under: $USER, unless that has other characters.
_KEYCHAIN_ACCOUNT = re.compile(r"[a-zA-Z0-9._-]+")
_KEYCHAIN_FALLBACK_ACCOUNT = "claude-code-user"
_KEYCHAIN_NOT_FOUND = 44  # `security` exit status for errSecItemNotFound
# The longest command the CLI pipes into `security -i`; past it, it uses argv.
_KEYCHAIN_STDIN_MAX = 4032

# Trusted Devices: the enrolled device's token sits at the document's top
# level next to the OAuth block, with a stamp when a refused call (not a
# sign-in) enrolled it; a refused token that a refusal enrolled less than a day
# ago isn't replaced by enrolling again (the CLI's guard against a loop).
TRUSTED_DEVICES_PATH = "/api/auth/trusted_devices"
_ENROLLED_AFTER_REFUSAL = "trustedDeviceEnrolledAfterRefusalAtMs"
_REFUSAL_ENROLL_COOLDOWN_MS = 24 * 3600 * 1000

# The CLI's bounds on a `refreshTokenExpiresAt` worth keeping: an epoch-ms time
# from 2025 on and at most ten years ahead.
_LOGIN_END_MIN_MS = 1735689600000
_LOGIN_END_MAX_AHEAD_MS = 3650 * 24 * 3600 * 1000

# Environment overrides (handy in CI / other machines).
ENV_ACCESS_TOKEN = "CLAUDE_RC_ACCESS_TOKEN"
ENV_ORG_UUID = "CLAUDE_RC_ORG_UUID"
# The CLI's own env override for a bare access token.
ENV_CLI_OAUTH_TOKEN = "CLAUDE_CODE_OAUTH_TOKEN"
# The CLI's trusted-device token override: sent instead of the stored token,
# and while it is set nothing enrolls.
ENV_TRUSTED_DEVICE_TOKEN = "CLAUDE_TRUSTED_DEVICE_TOKEN"


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
    # Keychain service the tokens came from (macOS); mutually exclusive with
    # ``source_path``. Either one marks the credentials as re-readable.
    source_keychain: Optional[str] = None
    # Set while a refresh of these tokens isn't saved (it failed, or persist
    # was off): the spent refresh token the store still holds. The store is
    # this login, just behind, so it isn't reloaded over these tokens, and
    # the next refresh may still save over it.
    stale_refresh_token: Optional[str] = None

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
        source_keychain: Optional[str] = None,
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
            source_keychain=source_keychain,
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
      2. macOS only, when no explicit ``path`` is given: the login-Keychain
         item :data:`KEYCHAIN_SERVICE`, which the CLI also reads first.
      3. ``~/.claude/.credentials.json`` (``claudeAiOauth`` block).
    """
    env_token = os.environ.get(ENV_ACCESS_TOKEN) or os.environ.get(ENV_CLI_OAUTH_TOKEN)
    if env_token:
        return OAuthCredentials(
            access_token=env_token, trusted_device_token=os.environ.get(ENV_TRUSTED_DEVICE_TOKEN)
        )

    keychain_miss = None
    if path is None and sys.platform == "darwin":
        try:
            raw = _keychain_read(KEYCHAIN_SERVICE)
        except CredentialsError as exc:  # locked, timed out: fall back to the file, as the CLI does
            raw, keychain_miss = None, str(exc)
        if raw is not None:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:  # the CLI reads an unparsable item as none too
                keychain_miss = f"Keychain item {KEYCHAIN_SERVICE!r} is not valid JSON"
            else:
                where = f"macOS Keychain item {KEYCHAIN_SERVICE!r}"
                return _from_document(data, where, source_keychain=KEYCHAIN_SERVICE)
        keychain_miss = keychain_miss or f"no {KEYCHAIN_SERVICE!r} Keychain item"

    p = Path(path) if path else DEFAULT_CREDENTIALS_PATH
    if not p.exists():
        where = f"credentials file not found at {p}"
        if keychain_miss:
            where = f"{keychain_miss}, and {where}"
        raise CredentialsError(
            f"{where}. Run `claude` and `/login` first, or set {ENV_ACCESS_TOKEN}."
        )
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as exc:  # pragma: no cover - io
        raise CredentialsError(f"could not read {p}: {exc}") from exc
    return _from_document(data, str(p), source_path=p)


def _from_document(
    data: dict,
    where: str,
    *,
    source_path: Optional[Path] = None,
    source_keychain: Optional[str] = None,
) -> OAuthCredentials:
    """Build credentials from the CLI's credentials JSON document."""
    block = data.get("claudeAiOauth")
    if not block:
        raise CredentialsError(
            f"{where} has no `claudeAiOauth` block — are you logged in via an API key "
            f"instead of claude.ai? Remote Control requires a claude.ai OAuth login."
        )
    # A top-level `trustedDeviceToken` sibling is used when the org requires
    # Trusted Devices; harmless to carry when absent.
    return OAuthCredentials.from_block(
        block,
        source_path=source_path,
        trusted_device_token=os.environ.get(ENV_TRUSTED_DEVICE_TOKEN) or data.get("trustedDeviceToken"),
        source_keychain=source_keychain,
    )


def _keychain_account() -> str:
    """The account the CLI files its item under: ``$USER`` (else the passwd
    name), or ``claude-code-user`` when that has characters it won't use."""
    try:
        import pwd  # only reached on macOS; Windows has no pwd

        name = os.environ.get("USER") or pwd.getpwuid(os.getuid()).pw_name
    except Exception:  # no USER and no passwd entry
        name = ""
    return name if _KEYCHAIN_ACCOUNT.fullmatch(name) else _KEYCHAIN_FALLBACK_ACCOUNT


def _keychain_read(service: str) -> Optional[str]:
    """The item's secret, or ``None`` when there is no such item. Raises
    :class:`CredentialsError` when the Keychain can't be read (locked, timed
    out), so a caller never mistakes that for an empty store."""
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-a", _keychain_account(), "-s", service, "-w"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CredentialsError(f"could not read Keychain item {service!r}: {exc}") from exc
    if out.returncode == _KEYCHAIN_NOT_FOUND:
        return None
    if out.returncode != 0:
        raise CredentialsError(
            f"could not read Keychain item {service!r}: security exited {out.returncode}"
            f" {out.stderr.strip()[:200]}"
        )
    return out.stdout.strip() or None


def _keychain_write(service: str, secret: str) -> None:
    """Create or update (``-U``) the item the way the CLI does: hex-encoded
    (``-X``) over ``security -i``'s stdin, out of reach of ``ps``. A command
    too long for ``security -i`` goes on argv instead, as the CLI's does (a
    credentials document holding MCP logins easily passes the limit)."""
    account, hexed = _keychain_account(), secret.encode().hex()
    cmd = f'add-generic-password -U -a "{account}" -s "{service}" -X "{hexed}"\n'
    if len(cmd) <= _KEYCHAIN_STDIN_MAX:
        argv, io = ["security", "-i"], {"input": cmd}
    else:
        argv = ["security", "add-generic-password", "-U", "-a", account, "-s", service, "-X", hexed]
        io = {"stdin": subprocess.DEVNULL}
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=15, **io)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CredentialsError(f"could not update Keychain item {service!r}: {exc}") from exc
    if out.returncode != 0:
        raise CredentialsError(
            f"could not update Keychain item {service!r}: {out.stderr.strip()[:200]}"
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

    Mirrors the CLI: ``POST`` to :data:`OAUTH_TOKEN_URL` with a JSON body of
    ``{grant_type, refresh_token, client_id}`` and the ``oauth-2025-04-20`` beta
    header. With ``persist``, the new tokens are written back to the store the
    credentials came from (file or Keychain item) so subsequent runs, and the
    CLI, stay authenticated. The refresh token is single-use and shared with
    the CLI, so, as the CLI's refresh does, it is spent only while the store
    still holds this login and can be read: a locked Keychain, a sign-in or
    sign-out since, another process's refresh, or a corrupt file raises
    :class:`CredentialsError` first. If saving fails after the refresh, the
    new tokens are still returned, with a logged warning and
    :attr:`~OAuthCredentials.stale_refresh_token` set: they are the only
    valid ones left.
    """
    if not creds.refresh_token:
        raise CredentialsError(
            "access token is expired and no refresh_token is available "
            "(token came from an env var). Re-run `claude` to refresh it."
        )
    # A store left behind by an unsaved refresh is still this login, as is
    # the "" the CLI clears its copy of that spent token to.
    behind = (creds.stale_refresh_token, "") if creds.stale_refresh_token else ()
    if persist:
        _stored_login(creds, (creds.refresh_token, *behind))

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
    lifetime = body.get("refresh_token_expires_in")
    login_ends_ms = _now_ms() + int(lifetime * 1000) if _is_number(lifetime) else None
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
        source_keychain=creds.source_keychain,
        stale_refresh_token=creds.stale_refresh_token or creds.refresh_token,
    )

    if persist:
        try:
            # Like the CLI's compare-and-swap, which also takes a "" it cleared.
            _persist(new, held=(creds.refresh_token, "", *behind), login_ends_ms=login_ends_ms)
            new.stale_refresh_token = None
        except CredentialsError as exc:  # never raise here: `new` is the only valid pair
            _log.warning("refreshed tokens kept in memory only: %s", exc)
    return new


def _stored_login(creds: OAuthCredentials, held: tuple) -> Optional[dict]:
    """The document ``creds`` came from, provided its login's refresh token is
    one of ``held`` (``None`` when there's no store). Raises
    :class:`CredentialsError` when the store can't be read or has moved on."""
    data = _read_document(creds)
    if data is None:
        return None
    block = data.get("claudeAiOauth")
    if not isinstance(block, dict) or block.get("refreshToken") not in held:
        where = f"Keychain item {creds.source_keychain!r}" if creds.source_keychain else creds.source_path
        raise CredentialsError(
            f"{where} no longer holds the login being refreshed (refreshed, signed in"
            " or out elsewhere, or unreadable), so it was left alone"
        )
    return data


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _login_end(value: object) -> Optional[float]:
    """``value`` if it is a ``refreshTokenExpiresAt`` the CLI would keep."""
    if _is_number(value) and _LOGIN_END_MIN_MS <= value <= _now_ms() + _LOGIN_END_MAX_AHEAD_MS:
        return value
    return None


def _persist(creds: OAuthCredentials, *, held: tuple, login_ends_ms: Optional[int] = None) -> None:
    """Write refreshed tokens back to the store they came from, the way the
    CLI's refresh saves: a compare-and-swap that writes only while the stored
    login's refresh token is one of ``held`` (the one just spent, or what the
    store was left with). Only the token fields change; the block's other
    fields (``subscriptionType``, ``rateLimitTier`` …) and the document's
    other keys (``mcpOAuth`` …) stay as stored.

    ``refreshTokenExpiresAt`` is when the login as a whole ends (the CLI warns
    as it nears). A refresh never moves it later: the stored time stays, or
    drops to ``login_ends_ms`` (the response's ``refresh_token_expires_in``)
    when that is sooner. A stored value the CLI wouldn't trust is removed."""
    data = _stored_login(creds, held)
    if data is None:
        return
    block = data["claudeAiOauth"]
    stored_end = _login_end(block.get("refreshTokenExpiresAt"))
    if stored_end is None:
        block.pop("refreshTokenExpiresAt", None)
    else:
        block["refreshTokenExpiresAt"] = min(stored_end, _login_end(login_ends_ms) or stored_end)
    block.update(
        accessToken=creds.access_token,
        refreshToken=creds.refresh_token,
        expiresAt=creds.expires_at_ms,
        scopes=creds.scopes,
    )
    _write_document(creds, data)


def _read_document(creds: OAuthCredentials) -> Optional[dict]:
    """The document ``creds`` came from (the credentials file or the Keychain
    item): ``None`` when they came from no store, and ``{}`` when it is gone,
    unreadable or not a JSON object, as the CLI's store reads it. A Keychain
    that can't be read (locked, timed out) raises :class:`CredentialsError`
    instead of passing for empty."""
    if creds.source_keychain:
        raw = _keychain_read(creds.source_keychain)
    elif creds.source_path:
        try:
            raw = creds.source_path.read_text()
        except OSError:
            raw = None
    else:
        return None
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _write_document(creds: OAuthCredentials, data: dict) -> None:
    """Replace the document ``creds`` came from (the file atomically)."""
    if creds.source_keychain:
        _keychain_write(creds.source_keychain, json.dumps(data))
        return
    path = creds.source_path
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2))
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError as exc:
        with contextlib.suppress(OSError):
            tmp.unlink()  # it holds tokens
        raise CredentialsError(f"could not write {path}: {exc}") from exc


def enroll_trusted_device(
    creds: OAuthCredentials,
    *,
    base_url: str = "https://api.anthropic.com",
    persist: bool = True,
    client: httpx.Client | None = None,
    display_name: str | None = None,
    after_refusal: bool = False,
) -> str | None:
    """Enroll this device for Trusted Devices, returning the device token.

    Some organizations require Trusted Devices for Remote Control: calls that
    touch an elevated session (e.g. reading its event stream) are rejected
    with HTTP 403 ``untrusted_device`` unless the caller presents an
    ``X-Trusted-Device-Token`` header. The official CLI enrolls on demand when
    it hits that rejection ("lazy enrollment"); this mirrors that behavior.
    Each enrollment registers a device on the account and emails its owner.

    On success the token is stored on ``creds.trusted_device_token`` and
    (optionally) written back to the store the credentials came from as the
    top-level ``trustedDeviceToken`` key, exactly where the CLI keeps it, as
    long as that store still holds this login (same access token) and can be
    read; otherwise that is logged and the token serves from memory.
    ``after_refusal`` (a refused call prompted this) also stamps the time,
    which the client's guard against re-enrolling reads. Returns ``None``
    when no token came back (the server declined, e.g. the org does not use
    Trusted Devices, or the request failed), so callers can fall through to
    the original error.
    """
    import platform
    import socket

    if not creds.access_token:
        raise CredentialsError("cannot enroll a trusted device without an access token")

    owns_client = client is None
    client = client or httpx.Client(timeout=30.0)
    try:
        name = display_name or f"claude-rc-api on {socket.gethostname()} · {platform.system().lower()}"
        resp = client.post(
            base_url.rstrip("/") + TRUSTED_DEVICES_PATH,
            headers={
                "Authorization": f"Bearer {creds.access_token}",
                "Content-Type": "application/json",
                "anthropic-version": "2023-06-01",
                "User-Agent": f"claude-rc-api-python/{__version__}",
            },
            json={"display_name": name},
            timeout=10.0,
        )
    except httpx.HTTPError:
        return None
    finally:
        if owns_client:
            client.close()

    if resp.status_code not in (200, 201):
        return None
    try:
        token = resp.json().get("device_token")
    except (ValueError, AttributeError):
        token = None
    if not token or not isinstance(token, str):
        return None

    creds.trusted_device_token = token  # first: an unsaved token still serves this process
    if persist:
        try:  # never raise past here: the device exists now, and its owner has been emailed
            data = _read_document(creds)
            if data is not None:
                if not _holds_login(data, creds):
                    raise CredentialsError("the store no longer holds the login that enrolled")
                data["trustedDeviceToken"] = token
                if after_refusal:
                    data[_ENROLLED_AFTER_REFUSAL] = _now_ms()
                else:
                    data.pop(_ENROLLED_AFTER_REFUSAL, None)
                _write_document(creds, data)
        except CredentialsError as exc:
            _log.warning("trusted-device token kept in memory only: %s", exc)
    return token


def _holds_login(data: dict, creds: OAuthCredentials) -> bool:
    """Whether a stored document's login is the one ``creds`` hold (the CLI
    compares access tokens the same way before it writes for a login)."""
    block = data.get("claudeAiOauth")
    return isinstance(block, dict) and block.get("accessToken") == creds.access_token


def _stored_device_token(creds: OAuthCredentials) -> tuple[Optional[str], bool]:
    """The store's ``trustedDeviceToken`` and whether ``creds`` may enroll to
    replace it: only for the login the store holds (not after a sign-out, or
    for one a sign-in replaced), and, as the CLI guards against an enrollment
    loop, not while the token is one a refusal enrolled less than a day ago
    or its stamp is unusable."""
    data = _read_document(creds) or {}
    token = data.get("trustedDeviceToken")
    if not _holds_login(data, creds):
        return token, False
    if not token or _ENROLLED_AFTER_REFUSAL not in data:
        return token, True
    stamp = data[_ENROLLED_AFTER_REFUSAL]
    if not isinstance(stamp, int) or isinstance(stamp, bool) or not 0 < stamp <= _now_ms():
        return token, False
    return token, _now_ms() - stamp >= _REFUSAL_ENROLL_COOLDOWN_MS
