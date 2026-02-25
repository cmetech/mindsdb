import os
import hmac
import secrets
import hashlib
from http import HTTPStatus
from typing import Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.requests import Request

from mindsdb.utilities import log
from mindsdb.utilities.config import config

logger = log.getLogger(__name__)

SECRET_KEY = os.environ.get("AUTH_SECRET_KEY") or secrets.token_urlsafe(32)
# We store token (fingerprints) in memory, which means everyone is logged out if the process restarts
TOKENS = []


def get_pat_fingerprint(token: str) -> str:
    """Hash the token with HMAC-SHA256 using secret_key as pepper."""
    return hmac.new(SECRET_KEY.encode(), token.encode(), hashlib.sha256).hexdigest()


def generate_pat() -> str:
    logger.debug("Generating new auth token")
    token = "pat_" + secrets.token_urlsafe(32)
    TOKENS.append(get_pat_fingerprint(token))
    return token


def verify_pat(raw_token: str) -> bool:
    """Verify if the raw_token matches a stored fingerprint.
    Returns token_id if valid, None if not.
    """
    if not raw_token:
        return False
    fp = get_pat_fingerprint(raw_token)
    for stored_fp in TOKENS:
        if hmac.compare_digest(fp, stored_fp):
            return True
    return False


def revoke_pat(raw_token: str) -> bool:
    """Revoke raw_token from active tokens"""
    if not raw_token:
        return False
    fp = get_pat_fingerprint(raw_token)
    for stored_fp in TOKENS:
        if hmac.compare_digest(fp, stored_fp):
            TOKENS.remove(stored_fp)
            return True
    return False


class PATAuthMiddleware(BaseHTTPMiddleware):
    def _extract_bearer(self, request: Request) -> Optional[str]:
        h = request.headers.get("Authorization")
        if not h or not h.startswith("Bearer "):
            return None
        return h.split(" ", 1)[1].strip() or None

    async def dispatch(self, request: Request, call_next):
        if config.get("auth", {}).get("http_auth_enabled", False) is False:
            return await call_next(request)

        token = self._extract_bearer(request)
        if not token or not verify_pat(token):
            return JSONResponse({"detail": "Unauthorized"}, status_code=HTTPStatus.UNAUTHORIZED)

        request.state.user = config["auth"].get("username")
        return await call_next(request)


# Used by mysql protocol
def check_auth(username, password, scramble_func, salt, company_id, user_id, config, client_address=None):
    """
    Authenticate MySQL protocol connections.

    PRD-MySQL-Auth: Routes to OSCAR auth when config["oscar_mysql_auth"]["enabled"] is True.
    Otherwise, uses static username/password from config["auth"].

    Args:
        username: MySQL username
        password: MySQL password (may be bytes or str, or OSCAR API key when enabled)
        scramble_func: MySQL password scramble function
        salt: MySQL auth salt
        company_id: Company/tenant ID
        user_id: User ID from context
        config: Full MindsDB config dict
        client_address: Optional tuple (ip, port) from mysql_proxy.self.client_address

    Returns:
        Dict with keys:
        - success: True/False
        - username: Authenticated username (if success)
        - company_id: Company ID (if success)
        - user_id: User UUID (if success, from context or OSCAR auth)
        - user_type: Optional 'user'/'system' (if OSCAR auth enabled)
        - msg: Error message (if failure)
    """
    # PRD-MySQL-Auth: Route to OSCAR auth when enabled
    oscar_auth_config = config.get("oscar_mysql_auth", {})
    if oscar_auth_config.get("enabled"):
        try:
            from mindsdb.api.common.oscar_auth import check_oscar_auth

            # Get client host from address tuple
            client_host = "unknown"
            if client_address:
                try:
                    client_host = client_address[0] if isinstance(client_address, tuple) else str(client_address)
                except (IndexError, TypeError):
                    pass

            # Password field contains OSCAR API key when OSCAR auth is enabled
            # Byte handling is done inside check_oscar_auth for consistent generic errors
            return check_oscar_auth(
                username=username,
                api_key=password,
                config=oscar_auth_config,
                client_host=client_host,
            )
        except ImportError as e:
            logger.error(f"[OSCAR_AUTH] Failed to import oscar_auth module: {e}")
            # SECURITY: Return generic error to prevent information leakage
            safe_user = username if username else ""
            return {
                "success": False,
                "msg": f"Access denied for user '{safe_user}'@'{client_host}' (using password: YES)",
            }
        except Exception as e:
            logger.exception(f"[OSCAR_AUTH] Unexpected error in OSCAR auth: {e}")
            # SECURITY: Return generic error to prevent information leakage
            safe_user = username if username else ""
            return {
                "success": False,
                "msg": f"Access denied for user '{safe_user}'@'{client_host}' (using password: YES)",
            }

    # Existing static auth logic (when OSCAR auth disabled)
    try:
        hardcoded_user = config["auth"].get("username")
        hardcoded_password = config["auth"].get("password")
        if hardcoded_password is None:
            hardcoded_password = ""
        hardcoded_password_hash = scramble_func(hardcoded_password, salt)
        hardcoded_password = hardcoded_password.encode()

        if password is None:
            password = ""
        if isinstance(password, str):
            password = password.encode()

        if username != hardcoded_user:
            logger.warning(f"Check auth, user={username}: user mismatch")
            return {"success": False}

        if password != hardcoded_password and password != hardcoded_password_hash:
            logger.warning(f"check auth, user={username}: password mismatch")
            return {"success": False}

        logger.info(f"Check auth, user={username}: Ok")
        return {"success": True, "username": username, "company_id": company_id, "user_id": user_id}
    except Exception:
        logger.exception(f"Check auth, user={username}: ERROR")
        return {"success": False}
