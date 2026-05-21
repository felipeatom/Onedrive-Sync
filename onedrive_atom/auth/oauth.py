"""OAuth2 authentication via MSAL — device code flow for desktop Linux."""

import logging
import os
import threading
import webbrowser
from pathlib import Path
from typing import Callable

import msal

from onedrive_atom.config import CONFIG_DIR, get_config

log = logging.getLogger(__name__)

# Minimum logging from MSAL itself so we can see what's happening
logging.getLogger("msal").setLevel(logging.DEBUG)


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

            # Notify the GUI (shows the dialog with code)
            on_message(user_code, verification_uri, expires_in)

            # Open browser immediately — every second saved matters
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
    """Silently acquire a fresh access token for an existing account."""
    cfg = get_config()
    cache = _load_cache(account_id)
    app = _build_app(cache)

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

    _save_cache(cache, account_id)
    return result.get("access_token")


def remove_account_tokens(account_id: str):
    path = _token_cache_path(account_id)
    if path.exists():
        path.unlink()
