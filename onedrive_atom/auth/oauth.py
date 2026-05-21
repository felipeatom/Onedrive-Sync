"""OAuth2 authentication via MSAL — device code flow for desktop Linux."""

import logging
import os
import threading
import time
import webbrowser
from pathlib import Path
from typing import Callable

import msal

from onedrive_atom.config import CONFIG_DIR, get_config

log = logging.getLogger(__name__)

# Avoid leaking token fragments from the MSAL library into application logs.
logging.getLogger("msal").setLevel(logging.WARNING)

# Per-account MSAL app instances — keeps the token cache in memory so we
# don't deserialize from disk on every HTTP request.
_msal_apps: dict[str, msal.PublicClientApplication] = {}
_msal_apps_lock = threading.Lock()

# Short-lived access token cache — avoids MSAL overhead on every API call.
# Entries: account_id -> (token, expires_at_monotonic)
_access_tokens: dict[str, tuple[str, float]] = {}
_access_tokens_lock = threading.Lock()


def _token_cache_path(account_id: str) -> Path:
    safe = account_id.replace("@", "_").replace(".", "_").replace("/", "_")
    return CONFIG_DIR / f"token_cache_{safe}.json"


def _load_cache(account_id: str) -> msal.SerializableTokenCache:
    cache = msal.SerializableTokenCache()
    path = _token_cache_path(account_id)
    if path.exists():
        cache.deserialize(path.read_text())
    return cache


def _save_cache(cache: msal.SerializableTokenCache, account_id: str):
    if cache.has_state_changed:
        path = _token_cache_path(account_id)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(cache.serialize())
        os.chmod(path, 0o600)


def _build_app(cache: msal.SerializableTokenCache) -> msal.PublicClientApplication:
    cfg = get_config()
    return msal.PublicClientApplication(
        client_id=cfg.client_id,
        authority=cfg.authority,
        token_cache=cache,
    )


def _get_or_build_app(account_id: str) -> msal.PublicClientApplication:
    with _msal_apps_lock:
        if account_id not in _msal_apps:
            cache = _load_cache(account_id)
            _msal_apps[account_id] = _build_app(cache)
        return _msal_apps[account_id]


def invalidate_account_cache(account_id: str):
    """Remove the cached MSAL app and access token for an account."""
    with _msal_apps_lock:
        _msal_apps.pop(account_id, None)
    with _access_tokens_lock:
        _access_tokens.pop(account_id, None)


def device_code_login(
    on_message: Callable[[str, str, int], None],
    on_success: Callable[[dict], None],
    on_error: Callable[[str], None],
):
    """
    Device code flow — no redirect URI required.
    on_message(user_code, verification_uri, expires_in) fires when the code is ready.
    Automatically opens the browser so the user saves time entering the code.
    """
    cfg = get_config()
    cache = msal.SerializableTokenCache()
    app = _build_app(cache)

    def _do_login():
        try:
            log.info("Initiating device flow with scopes: %s", cfg.scopes)
            flow = app.initiate_device_flow(scopes=cfg.scopes)
            log.debug("Device flow response keys: %s", list(flow.keys()))

            if "error" in flow:
                msg = flow.get("error_description") or flow["error"]
                log.error("initiate_device_flow error: %s", msg)
                on_error(msg)
                return

            user_code = flow.get("user_code", "")
            expires_in = int(flow.get("expires_in", 900))
            verification_uri = "https://microsoft.com/devicelogin"

            log.info("Device code ready. user_code=%s expires_in=%ds", user_code, expires_in)

            on_message(user_code, verification_uri, expires_in)
            webbrowser.open(verification_uri)

            log.info("Polling for token (will poll for up to %ds)…", expires_in)
            result = app.acquire_token_by_device_flow(flow)
            log.debug("acquire_token_by_device_flow result keys: %s", list(result.keys()))

            if "error" in result:
                err = result.get("error", "")
                desc = result.get("error_description") or err
                log.error("Token error: %s — %s", err, desc)
                if err in ("expired_token", "code_expired", "authorization_declined"):
                    on_error("__expired__")
                else:
                    on_error(desc)
                return

            account = result.get("id_token_claims", {})
            account_id = account.get("oid") or account.get("sub", "unknown")
            log.info("Login successful for oid=%s", account_id)
            _save_cache(cache, account_id)
            # Ensure next get_access_token call picks up the newly saved cache.
            invalidate_account_cache(account_id)

            on_success({
                "account_id": account_id,
                "email": account.get("preferred_username") or account.get("upn") or account.get("email", ""),
                "name": account.get("name", ""),
                "tenant_id": account.get("tid", ""),
                "token_cache_path": str(_token_cache_path(account_id)),
            })

        except Exception as e:
            log.exception("Unexpected error in device_code_login thread: %s", e)
            on_error(f"Erro inesperado: {e}")

    threading.Thread(target=_do_login, daemon=False, name="device-code-login").start()


def get_access_token(account_id: str) -> str | None:
    """Silently acquire a fresh access token, using an in-memory cache to avoid
    repeated MSAL deserialization and disk I/O on every API request."""
    with _access_tokens_lock:
        if account_id in _access_tokens:
            token, expires_at = _access_tokens[account_id]
            if time.monotonic() < expires_at:
                return token

    cfg = get_config()
    app = _get_or_build_app(account_id)

    accounts = app.get_accounts()
    if not accounts:
        log.warning("No MSAL accounts in cache for %s", account_id)
        return None

    result = app.acquire_token_silent(scopes=cfg.scopes, account=accounts[0])
    if not result:
        log.warning("Silent token acquisition failed for %s", account_id)
        return None

    if "error" in result:
        log.error("Token error for %s: %s", account_id, result.get("error_description"))
        return None

    _save_cache(app.token_cache, account_id)
    token = result.get("access_token", "")
    expires_in = result.get("expires_in", 3600)
    with _access_tokens_lock:
        _access_tokens[account_id] = (token, time.monotonic() + expires_in - 60)
    return token or None


def remove_account_tokens(account_id: str):
    path = _token_cache_path(account_id)
    if path.exists():
        path.unlink()
    invalidate_account_cache(account_id)
